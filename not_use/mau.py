"""
YumeAI — Gradio GUI
Chat (Ollama) + Tạo ảnh + img2img + ControlNet (giữ khuôn mặt/tư thế)

Cài: pip install gradio httpx pillow
Chạy: python app_gradio.py
"""

import gradio as gr
from yumeai.core.prompt_router import plan_request
print(">>> YumeAI app_full — BAN GOP DAY DU + PROMPT THONG MINH <<<  (da gop 2 ban)")

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

import httpx, json, uuid, time, io, random, os, re, inspect, base64

# V8.10: ComfyUI long timeout config
# Có thể chỉnh trong PowerShell trước khi chạy app:
#   $env:YUMEAI_COMFY_TIMEOUT_SEC="1800"
COMFY_LONG_TIMEOUT_SEC = int(os.getenv("YUMEAI_COMFY_TIMEOUT_SEC", "1800"))

# V8.12: Resource Guard / Low-VRAM safety config
# Có thể chỉnh trong PowerShell trước khi chạy app:
#   $env:YUMEAI_LOW_VRAM_MODE="1"              # 1=giảm tải, 0=giữ như cũ
#   $env:YUMEAI_FLUX_SAFE_STEPS="24"           # cap steps cho Flux thường
#   $env:YUMEAI_GARMENT_SAFE_STEPS="24"        # cap steps cho teacher/garment pipeline
#   $env:YUMEAI_TEACHER_RETRY_MAX="0"          # 0=không retry tự động để tránh chạy chồng lâu
#   $env:YUMEAI_RESOURCE_GUARD="1"             # 1=chỉ cho 1 job ComfyUI chạy cùng lúc

def _env_bool(name: str, default: bool = True) -> bool:
    v = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return v not in {"0", "false", "off", "no", "n"}


def _env_int(name: str, default: int, *, lo: int = None, hi: int = None) -> int:
    try:
        v = int(os.getenv(name, str(default)))
    except Exception:
        v = int(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


YUMEAI_LOW_VRAM_MODE = _env_bool("YUMEAI_LOW_VRAM_MODE", True)
YUMEAI_RESOURCE_GUARD = _env_bool("YUMEAI_RESOURCE_GUARD", True)
YUMEAI_FLUX_SAFE_STEPS = _env_int("YUMEAI_FLUX_SAFE_STEPS", 24, lo=8, hi=40)
YUMEAI_GARMENT_SAFE_STEPS = _env_int("YUMEAI_GARMENT_SAFE_STEPS", 24, lo=8, hi=40)
YUMEAI_TEACHER_RETRY_MAX = _env_int("YUMEAI_TEACHER_RETRY_MAX", 0 if YUMEAI_LOW_VRAM_MODE else 1, lo=0, hi=1)


def _resource_safe_steps(requested_steps, *, kind: str = "flux") -> int:
    """V8.12: cap steps để Flux/Kontext không ép laptop 4GB VRAM chạy quá lâu."""
    steps = max(8, min(40, int(requested_steps)))
    if not YUMEAI_LOW_VRAM_MODE:
        return steps
    cap = YUMEAI_GARMENT_SAFE_STEPS if kind in {"garment", "teacher"} else YUMEAI_FLUX_SAFE_STEPS
    capped = max(8, min(steps, int(cap)))
    if capped < steps:
        print(f"🛡️ V8.12 low-VRAM: cap {kind} steps {steps} → {capped}")
    return capped
from PIL import Image, ImageFilter
import numpy as np

# ══════════════════════════════════
# CẤU HÌNH + SERVICES (đã tách module)
# ══════════════════════════════════
from yumeai.core.config import OLLAMA_URL, COMFY_URL, OLLAMA_MODEL, COMFY_MODELS_DIR
from yumeai.core.personas import PERSONA
from yumeai.services.ollama_service import (
    TRANSLATE_MODEL,
    translate_vi_en,
    _ollama_chat,
    warmup_ollama,
    enrich_for_create,
)
from yumeai.services.comfy_service import (
    _fetch_model_list,
    _is_sdxl,
    get_checkpoint_model,
    get_controlnet_models,
    get_ipadapter_models,
    get_clipvision_models,
    pick_controlnet,
    pick_ipadapter,
    upload_image_to_comfy,
    submit_workflow,
    poll_and_download,
    get_reactor_available,
    get_upscale_models,
)


# ══════════════════════════════════
# V8.12 RESOURCE GUARD — chống quá tải/chồng job ComfyUI
# ══════════════════════════════════
_raw_submit_workflow = submit_workflow
_raw_poll_and_download = poll_and_download
_YUMEAI_JOB_LOCK = threading.RLock()
_YUMEAI_JOB_STATE = {"owner": None, "label": "", "start": 0.0, "depth": 0}


def _acquire_yumeai_generation_slot(label: str = "ComfyUI workflow") -> bool:
    """Non-blocking lock: đang có job thì báo bận, không gửi thêm workflow mới."""
    if not YUMEAI_RESOURCE_GUARD:
        return False
    tid = threading.get_ident()
    if _YUMEAI_JOB_STATE.get("owner") == tid:
        _YUMEAI_JOB_STATE["depth"] += 1
        return True
    if not _YUMEAI_JOB_LOCK.acquire(blocking=False):
        active = _YUMEAI_JOB_STATE.get("label") or "ComfyUI workflow"
        elapsed = int(max(0, time.time() - float(_YUMEAI_JOB_STATE.get("start") or time.time())))
        raise gr.Error(
            f"⚠️ YumeAI đang xử lý job khác: {active} ({elapsed}s).\n"
            "Để tránh tràn VRAM/RAM hoặc reset máy, chị đã chặn gửi thêm job ComfyUI. "
            "Đợi job hiện tại xong rồi bấm Gửi lại nha."
        )
    _YUMEAI_JOB_STATE.update({"owner": tid, "label": label, "start": time.time(), "depth": 1})
    print(f"🛡️ V8.12 Resource Guard: START {label}")
    return True


def _release_yumeai_generation_slot() -> None:
    if not YUMEAI_RESOURCE_GUARD:
        return
    tid = threading.get_ident()
    if _YUMEAI_JOB_STATE.get("owner") != tid:
        return
    _YUMEAI_JOB_STATE["depth"] = max(0, int(_YUMEAI_JOB_STATE.get("depth") or 1) - 1)
    if _YUMEAI_JOB_STATE["depth"] > 0:
        return
    label = _YUMEAI_JOB_STATE.get("label") or "ComfyUI workflow"
    elapsed = int(max(0, time.time() - float(_YUMEAI_JOB_STATE.get("start") or time.time())))
    _YUMEAI_JOB_STATE.update({"owner": None, "label": "", "start": 0.0, "depth": 0})
    try:
        _YUMEAI_JOB_LOCK.release()
    except RuntimeError:
        pass
    print(f"🛡️ V8.12 Resource Guard: END {label} ({elapsed}s)")


def submit_workflow(workflow, *args, **kwargs):
    """Wrapper giữ nguyên tên cũ để toàn file tự được bảo vệ."""
    _acquire_yumeai_generation_slot("ComfyUI workflow")
    try:
        return _raw_submit_workflow(workflow, *args, **kwargs)
    except Exception:
        _release_yumeai_generation_slot()
        raise


def poll_and_download(*args, **kwargs):
    """Luôn nhả lock kể cả khi timeout/lỗi để app không bị kẹt trạng thái bận."""
    try:
        return _raw_poll_and_download(*args, **kwargs)
    finally:
        _release_yumeai_generation_slot()



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
        with httpx.Client(timeout=180) as c:
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

    # Ollama vision model (cho garment caption V4)
    vis = _get_ollama_vision_model()
    if vis:
        lines.append(f"✅ Ollama Vision: {vis} (dùng cho teacher pipeline V7)")
    else:
        lines.append("⚠️  Ollama Vision: chưa có → V7 sẽ fallback sang teacher prompt / garment latent")

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


def _is_comfy_timeout_error(e) -> bool:
    msg = str(e).lower()
    return ("timeout" in msg and "comfy" in msg) or "comfyui chạy quá lâu" in msg or "timeout — comfyui" in msg


def _timeout_stop_info(stage: str, e, *, scope="", steps=None, timeout_sec=None) -> str:
    return (
        f"⏳ V8.12 STOP — ComfyUI timeout ở {stage}\n"
        f"❌ Lỗi: {e}\n"
        f"🎯 scope: {scope or 'unknown'} | steps: {steps or 'unknown'} | timeout: {timeout_sec or COMFY_LONG_TIMEOUT_SEC}s\n"
        "🛡️ Đã chặn fallback dây chuyền để tránh tạo thêm nhiều job ComfyUI chồng lên nhau.\n"
        "↩️ Ảnh hiện tại được giữ nguyên / rollback về ảnh an toàn gần nhất.\n"
        "Gợi ý test nhanh: giảm Steps xuống 20-28 hoặc tắt Turbo LoRA nếu thấy quality lạ; nếu vẫn chậm, tăng biến môi trường YUMEAI_COMFY_TIMEOUT_SEC.\n"
    )
# ══════════════════════════════════
# SMART EXECUTE — Bộ não đơn giản hoá UI
# ══════════════════════════════════

AUTO_PRESETS = {
    "text2img": {
        "quality": "Cân bằng (28 steps)",
        "upscale": "Không upscale",
    },
    "edit_clothes": {
        "flux_guidance": 3.5,
        "flux_steps": 28,
        "flux_turbo": True,
    },
    "style_edit": {
        "flux_guidance": 3.8,
        "flux_steps": 28,
        "flux_turbo": True,
    },
    "generic_edit": {
        "flux_guidance": 3.5,
        "flux_steps": 26,
        "flux_turbo": True,
    },
    "identity_edit": {
        "cn_mode": "Canny (giữ cấu trúc)",
        "cn_strength": 0.60,
        "face_strength": 0.70,
        "use_ipadapter": True,
        "use_reactor": False,
        "use_upscale": False,
    },
}


def build_smart_runners(
    *,
    use_ip=True,
    use_reactor=False,
    use_esrgan=False,
    cn_str=0.6,
    face_str=0.7,
    use_flux=True,
    flux_guidance=3.5,
    flux_steps=28,
    flux_turbo=True,
    flux_lora_strength=1.0,
    kontext_prompt="",
    kontext_review=True,
):
    """
    Gom các runner vào một dict.
    Bản này đã nối THẬT:
    - flux_edit  -> flux_kontext_fn
    - garment_transfer -> garment_transfer_fn
    Nếu tắt Flux trong UI, flux_edit sẽ fallback sang ControlNet/IP-Adapter.
    """

    runners = {}

    # 1) text2img
    runners["text2img"] = lambda prompt, style, progress: generate_image_fn(
        prompt=prompt,
        style=style,
        quality=AUTO_PRESETS["text2img"]["quality"],
        upscale=AUTO_PRESETS["text2img"]["upscale"],
        progress=progress,
    )

    # 2) inpaint
    runners["inpaint"] = lambda input_image, action, target_vi, replacement_vi, style, resolution, progress: inpaint_fn(
        input_image=input_image,
        action=action,
        target_vi=target_vi,
        replacement_vi=replacement_vi,
        style=style,
        resolution=resolution,
        progress=progress,
    )

    # 3) identity edit (giữ mặt/tư thế bằng ControlNet + IP-Adapter)
    runners["identity_edit"] = lambda input_image, prompt, style, resolution, progress: controlnet_fn(
        input_image=input_image,
        prompt=prompt,
        cn_mode=AUTO_PRESETS["identity_edit"]["cn_mode"],
        cn_strength=cn_str if cn_str is not None else AUTO_PRESETS["identity_edit"]["cn_strength"],
        face_strength=face_str if face_str is not None else AUTO_PRESETS["identity_edit"]["face_strength"],
        use_ipadapter=use_ip,
        use_reactor=use_reactor,
        use_upscale=use_esrgan,
        style=style,
        resolution=resolution,
        progress=progress,
    )

    # 4) fallback edit nếu không dùng/không có Flux
    runners["fallback_edit"] = lambda input_image, prompt, style, resolution, progress: controlnet_fn(
        input_image=input_image,
        prompt=prompt,
        cn_mode="Canny (giữ cấu trúc)",
        cn_strength=cn_str,
        face_strength=face_str,
        use_ipadapter=use_ip,
        use_reactor=use_reactor,
        use_upscale=use_esrgan,
        style=style,
        resolution=resolution,
        progress=progress,
    )

    # 5) Flux edit — pipeline chính cho "đổi đồ / đổi style / đổi nền"
    def _flux_edit_runner(input_image, prompt, style, resolution, progress):
        if not use_flux:
            print("ℹ️ UI đang tắt Flux Kontext → fallback sang ControlNet/IP-Adapter")
            return runners["fallback_edit"](input_image, prompt, style, resolution, progress)

        try:
            # V7: mọi yêu cầu đổi trang phục đi qua teacher pipeline.
            if _looks_like_clothes_request(prompt):
                if progress:
                    progress(0.04, desc="🧭 V7: phân tích ảnh hiện tại...")
                scene_desc_en, scene_model = describe_scene_image(input_image, prompt)
                plan = build_teacher_plan_json(prompt, scene_desc_en=scene_desc_en, outfit_caption_en="", scope="all")
                instruction_en = compile_teacher_instruction(plan)
                print(f"🧠 V7 teacher plan ({scene_model or 'no-vision'}): {json.dumps(plan, ensure_ascii=False)}")
                return run_flux_teacher_v7(
                    input_image,
                    prompt,
                    instruction_en,
                    progress=progress,
                    guidance=max(4.6, float(flux_guidance)),
                    steps=max(32, int(flux_steps)),
                    lora_strength=flux_lora_strength,
                    scope="all",
                    outfit_caption_en="",
                    scene_desc_en=scene_desc_en,
                    auto_verify=True,
                )

            return flux_kontext_fn(
                input_image=input_image,
                instruction_vi=prompt,
                progress=progress,
                guidance=flux_guidance,
                steps=flux_steps,
                use_turbo=flux_turbo,
                lora_strength=flux_lora_strength,
                instruction_override=kontext_prompt,
                review=kontext_review,
            )
        except gr.Error:
            raise
        except Exception as e:
            if _is_comfy_timeout_error(e):
                print(f"⏳ Flux Kontext/V7 timeout — STOP, không fallback ControlNet để tránh chồng job: {e}")
                raise
            print(f"⚠️ Flux Kontext/V7 lỗi → fallback ControlNet/IP-Adapter: {e}")
            return runners["fallback_edit"](input_image, prompt, style, resolution, progress)


    runners["flux_edit"] = _flux_edit_runner

    # 6) Garment transfer — chỉ dùng khi có ảnh trang phục và người dùng thật sự muốn "mặc theo ảnh"
    def _garment_transfer_runner(character_image, outfit_image, prompt, style, resolution, progress, scope="all", strict=True):
        scope = _normalize_scope(scope)
        if not use_flux:
            raise gr.Error("Tính năng mặc theo ảnh trang phục cần bật Flux Kontext.")

        if progress:
            progress(0.04, desc="🧭 V7: phân tích ảnh hiện tại...")
        scene_desc_en, scene_model = describe_scene_image(character_image, prompt)

        outfit_caption_en = ""
        vision_model = ""
        same_image = False
        if outfit_image is not None:
            same_image = _images_maybe_same(character_image, outfit_image)
            if same_image:
                print("⚠️ Ảnh outfit gần như trùng ảnh hiện tại → không dùng reference outfit, dùng teacher prompt text.")
            else:
                if progress:
                    progress(0.07, desc="👁️ V7: đọc ảnh trang phục tham chiếu...")
                outfit_caption_en, vision_model = describe_outfit_image(outfit_image, prompt)

        plan = build_teacher_plan_json(prompt, scene_desc_en=scene_desc_en, outfit_caption_en=outfit_caption_en, scope=scope)
        remove_list = plan.get("source_only_remove_list") or []
        instruction_en = compile_teacher_instruction(plan)
        print(f"👕 V8 garment teacher plan ({scene_model or 'no-scene'} | outfit:{vision_model or 'none'} | {scope}): {json.dumps(plan, ensure_ascii=False)}")
        print(f"🧼 V8.5 source-only remove_list: {remove_list}")

        try:
            n_chars = _estimate_visible_character_count(scene_desc_en)
            if scope == "all" and n_chars >= 2 and outfit_caption_en:
                base_image = character_image
                last_good_image = character_image
                if progress:
                    progress(0.10, desc="🪄 V8.10: pass 1/2 cho nhân vật bên trái...")
                left_plan = build_teacher_plan_json(prompt, scene_desc_en=scene_desc_en, outfit_caption_en=outfit_caption_en, scope="left")
                left_inst = compile_teacher_instruction(left_plan) + " " + build_source_only_cleanup_instruction(remove_list, "left") + " Keep the right character unchanged in this pass. Keep exactly the same number of visible characters as the input image. Do not remove or hide any character. Only edit the left character clothing region."
                img1, info1 = run_flux_teacher_v7(
                    character_image,
                    prompt,
                    left_inst,
                    progress=progress,
                    guidance=max(5.1, float(flux_guidance)),
                    steps=max(36, int(flux_steps)),
                    lora_strength=flux_lora_strength,
                    scope="left",
                    outfit_caption_en=outfit_caption_en,
                    scene_desc_en=scene_desc_en,
                    auto_verify=True,
                )
                if "SAFE-COMMIT REJECT" in (info1 or ""):
                    header = (
                        f"👕 Mặc theo ảnh/trang phục (V8.10 safe-commit stop after LEFT pass)\n"
                        f"🎯 Scope: {_scope_label_vi(scope)}\n"
                        f"🧭 scene_vision:{scene_model}\n"
                        f"👁️ outfit_vision:{vision_model or 'none'}\n"
                        f"⚠️ same_reference:{same_image}\n📌 outfit_caption_empty:{not bool(outfit_caption_en)}\n🧼 remove_list:{remove_list}\n"
                    )
                    return base_image, header + "\n=== PASS LEFT ===\n" + info1
                last_good_image = img1

                if progress:
                    progress(0.55, desc="🪄 V8.10: pass 2/2 cho nhân vật bên phải...")
                scene_desc_en2, scene_model2 = describe_scene_image(last_good_image, prompt)
                scene_desc_for_pass2 = scene_desc_en2 or scene_desc_en  # V8.11: dùng scene sau PASS LEFT để tránh anchor cũ làm pass phải bị lệch
                right_plan = build_teacher_plan_json(prompt, scene_desc_en=scene_desc_for_pass2, outfit_caption_en=outfit_caption_en, scope="right")
                right_inst = compile_teacher_instruction(right_plan) + " " + build_source_only_cleanup_instruction(remove_list, "right") + " Keep the left character's new clothes unchanged in this pass. Keep exactly the same number of visible characters as the input image. Do not remove or hide any character. Only edit the right character clothing region."
                img2, info2 = run_flux_teacher_v7(
                    last_good_image,
                    prompt,
                    right_inst,
                    progress=progress,
                    guidance=max(5.2, float(flux_guidance)),
                    steps=max(36, int(flux_steps)),
                    lora_strength=flux_lora_strength,
                    scope="right",
                    outfit_caption_en=outfit_caption_en,
                    scene_desc_en=scene_desc_for_pass2,
                    auto_verify=True,
                )
                if "SAFE-COMMIT REJECT" in (info2 or ""):
                    header = (
                        f"👕 Mặc theo ảnh/trang phục (V8.10 safe-commit stop after RIGHT pass)\n"
                        f"🎯 Scope: {_scope_label_vi(scope)}\n"
                        f"🧭 scene_vision:{scene_model}\n"
                        f"👁️ outfit_vision:{vision_model or 'none'}\n"
                        f"⚠️ same_reference:{same_image}\n📌 outfit_caption_empty:{not bool(outfit_caption_en)}\n🧼 remove_list:{remove_list}\n"
                        "↩️ Trả lại last_good_image sau PASS LEFT vì PASS RIGHT chưa đạt.\n"
                    )
                    return last_good_image, header + "\n=== PASS LEFT ===\n" + info1 + "\n\n=== PASS RIGHT ===\n" + info2
                last_good_image = img2

                final_verify = verify_image_edit_result(last_good_image, prompt, scope="all", outfit_caption_en=outfit_caption_en, scene_desc_en=scene_desc_en, original_image=character_image)
                header = (
                    f"👕 Mặc theo ảnh/trang phục (V8.10 teacher pipeline — multi-character 2-pass safe-commit)\n"
                    f"🎯 Scope: {_scope_label_vi(scope)}\n"
                    f"🧭 scene_vision:{scene_model}\n"
                    f"👁️ outfit_vision:{vision_model or 'none'}\n"
                    f"⚠️ same_reference:{same_image}\n📌 outfit_caption_empty:{not bool(outfit_caption_en)}\n🧼 remove_list:{remove_list}\n"
                    "🛡️ final_cleanup_global: disabled (safe-commit mode)\n"
                )
                info = header + "\n=== PASS LEFT ===\n" + info1 + "\n\n=== PASS RIGHT ===\n" + info2 + f"\n\n🧪 final_verify: {final_verify}"
                return last_good_image, info

            img, info = run_flux_teacher_v7(
                character_image,
                prompt,
                instruction_en,
                progress=progress,
                guidance=max(5.0, float(flux_guidance)),
                steps=max(32, int(flux_steps)),
                lora_strength=flux_lora_strength,
                scope=scope,
                outfit_caption_en=outfit_caption_en,
                scene_desc_en=scene_desc_en,
                auto_verify=True,
            )
            header = (
                f"👕 Mặc theo ảnh/trang phục (V8.7 teacher pipeline)\n"
                f"🎯 Scope: {_scope_label_vi(scope)}\n"
                f"🧭 scene_vision:{scene_model}\n"
                f"👁️ outfit_vision:{vision_model or 'none'}\n"
                f"⚠️ same_reference:{same_image}\n📌 outfit_caption_empty:{not bool(outfit_caption_en)}\n🧼 remove_list:{remove_list}\n"
            )
            return img, header + info
        except gr.Error:
            raise
        except Exception as e:
            if _is_comfy_timeout_error(e):
                print(f"⏳ V8.12 teacher pipeline timeout — STOP, không fallback dây chuyền: {e}")
                return character_image, _timeout_stop_info("teacher pipeline / Flux Kontext", e, scope=scope, steps=flux_steps)
            print(f"⚠️ V8.12 teacher pipeline lỗi → fallback garment latent HQ: {e}")



        # Fallback cuối: garment reference latent HQ CHỈ khi có outfit thật và caption outfit đọc được.
        # Nếu caption outfit rỗng / timeout / reference gần giống current thì KHÔNG ép latent fallback nữa,
        # vì rất dễ ra ảnh giữ nguyên hoặc bám sai tham chiếu.
        if outfit_image is not None and (not same_image) and outfit_caption_en:
            try:
                img, info = garment_transfer_fn(
                    person_image=character_image,
                    garment_image=outfit_image,
                    note_vi=prompt,
                    progress=progress,
                    guidance=max(5.2, float(flux_guidance)),
                    steps=max(28, int(flux_steps)),
                    use_turbo=False,
                    lora_strength=flux_lora_strength,
                )
                info = f"🎯 Scope: {_scope_label_vi(scope)}\n⚠️ Fallback garment latent HQ\n" + info
                return img, info
            except gr.Error:
                raise
            except Exception as e:
                if _is_comfy_timeout_error(e):
                    print(f"⏳ Garment latent timeout — STOP, không chạy fallback text tiếp: {e}")
                    return character_image, _timeout_stop_info("fallback garment latent", e, scope=scope, steps=flux_steps)
                print(f"⚠️ Garment latent cũng lỗi: {e}")
        else:
            print("ℹ️ Bỏ qua garment latent fallback vì reference không đủ tin cậy (same image hoặc caption outfit rỗng).")

        # Fallback cuối cùng: Flux edit text teacher không reference
        fallback_plan = build_teacher_plan_json(prompt, scene_desc_en=scene_desc_en, outfit_caption_en="", scope=scope)
        fallback_inst = compile_teacher_instruction(fallback_plan)
        return run_flux_teacher_v7(
            character_image,
            prompt,
            fallback_inst,
            progress=progress,
            guidance=max(4.8, float(flux_guidance)),
            steps=max(28, int(flux_steps)),
            lora_strength=flux_lora_strength,
            scope=scope,
            outfit_caption_en="",
            scene_desc_en=scene_desc_en,
            auto_verify=False,
        )


    runners["garment_transfer"] = _garment_transfer_runner

    # 7) Region edit — chỉ dùng khi người dùng bấm nút "dùng vùng này cho lần gửi tới"
    runners["region_edit"] = lambda input_image, target_vi, new_outfit_vi, style, resolution, progress: region_edit_fn(
        input_image=input_image,
        target_vi=target_vi,
        new_outfit_vi=new_outfit_vi,
        style=style,
        resolution=resolution,
        progress=progress,
    )

    return runners


def smart_execute(
    user_prompt: str,
    input_image=None,
    outfit_image=None,
    *,
    default_style="Anime",
    resolution="512",
    progress=_dummy_progress,
    runners=None,
    tool_once=None,
):
    """
    Bộ điều phối chính:
    - hiểu prompt
    - tự chọn pipeline
    - gọi runner phù hợp
    """
    text = (user_prompt or "").strip()
    has_image = input_image is not None
    has_outfit = outfit_image is not None

    if not text and not has_image:
        raise gr.Error("Hãy nhập prompt hoặc upload ảnh.")

    if runners is None:
        runners = build_smart_runners()

    # ── One-shot tool override từ bảng chức năng.
    # Bảng chức năng KHÔNG chạy riêng nữa; nó chỉ đặt mode cho đúng 1 lần bấm Gửi.
    tool_once = tool_once or {"mode": "auto"}
    requested_tool_mode = (tool_once.get("mode") or "auto").lower()
    effective_mode, mode_meta = choose_best_edit_mode(
        tool_once.get("note") or text,
        input_image=input_image,
        outfit_image=outfit_image,
        requested_mode=requested_tool_mode,
    )
    print("🧭 V8 MODE ROUTER:", json.dumps(mode_meta, ensure_ascii=False))
    tool_mode = effective_mode if requested_tool_mode in {"garment", "auto"} else requested_tool_mode

    if tool_mode == "region":
        if input_image is None:
            raise gr.Error("Chưa có ảnh hiện tại để đổi vùng.")
        target = (tool_once.get("target") or "").strip()
        new_outfit = (tool_once.get("new") or "").strip()
        if not target or not new_outfit:
            raise gr.Error("Thiếu vùng cần đổi hoặc đồ mới trong bảng Đổi đồ theo vùng.")
        img, info = runners["region_edit"](input_image, target, new_outfit, default_style, resolution, progress)
        plan = {
            "task": "region_once",
            "style": default_style,
            "target": target,
            "replacement": new_outfit,
            "use_flux": False,
            "reason": "One-shot từ bảng Đổi đồ theo vùng."
        }
        return {"task": "region_once", "plan": plan, "assistant_text": None, "image": img, "info": info}

    if tool_mode == "garment":
        if input_image is None:
            raise gr.Error("Chưa có ảnh nhân vật hiện tại để mặc theo ảnh trang phục.")
        if outfit_image is None:
            raise gr.Error("Chưa có ảnh trang phục tham chiếu.")
        note = (tool_once.get("note") or text or "mặc theo ảnh trang phục tham chiếu").strip()
        scope = _normalize_scope(tool_once.get("scope") or "all")
        strict = bool(tool_once.get("strict", True))
        img, info = runners["garment_transfer"](input_image, outfit_image, note, default_style, resolution, progress, scope=scope, strict=strict)
        plan = {
            "task": "garment_once",
            "style": default_style,
            "use_flux": True,
            "need_outfit_image": True,
            "scope": scope,
            "strict": strict,
            "current_image_tag": _image_debug_tag(input_image),
            "outfit_image_tag": _image_debug_tag(outfit_image),
            "same_reference": _images_maybe_same(input_image, outfit_image),
            "reason": "One-shot từ bảng Mặc theo ảnh trang phục (V8.10 safe-commit router + teacher pipeline).",
            "mode_router": mode_meta,
        }
        return {"task": "garment_once", "plan": plan, "assistant_text": None, "image": img, "info": info}

    if tool_mode == "flux_edit" and input_image is not None and _looks_like_clothes_request(tool_once.get("note") or text):
        note = (tool_once.get("note") or text or "đổi trang phục theo yêu cầu").strip()
        img, info = runners["flux_edit"](input_image, note, default_style, resolution, progress)
        plan = {
            "task": "edit_clothes",
            "style": default_style,
            "use_flux": True,
            "reason": "V8 router tự chuyển từ garment/reference sang text clothes edit để tránh bám nhầm ảnh tham chiếu.",
            "mode_router": mode_meta,
        }
        return {"task": "edit_clothes", "plan": plan, "assistant_text": None, "image": img, "info": info}

    plan = plan_request(
        text=text,
        has_image=has_image,
        has_outfit_image=has_outfit,
        default_style=default_style,
        use_llm_fallback=True,
    )

    print("🧠 SMART PLAN:", json.dumps(plan, ensure_ascii=False, indent=2))

    task = plan.get("task", "chat")
    style = plan.get("style", default_style)

    # 1) Chat
    if task == "chat":
        return {
            "task": "chat",
            "plan": plan,
            "assistant_text": None,
            "image": None,
            "info": "Prompt được xem là chat thường.",
        }

    # 2) Text2Img
    if task == "text2img":
        img, info = runners["text2img"](text, style, progress)
        return {
            "task": task,
            "plan": plan,
            "assistant_text": None,
            "image": img,
            "info": info,
        }

    # 3) Inpaint
    if task == "inpaint":
        action = plan.get("action")
        target = plan.get("target")
        replacement = plan.get("replacement")

        if not action or not target:
            # fallback parse nhanh
            action, target, replacement = parse_inpaint_command(text)

        if not action or not target:
            raise gr.Error("Không phân tích được lệnh xóa/thay vật thể.")

        img, info = runners["inpaint"](
            input_image, action, target, replacement, style, resolution, progress
        )
        return {
            "task": task,
            "plan": plan,
            "assistant_text": None,
            "image": img,
            "info": info,
        }

    # 4) Garment transfer
    if task == "garment_transfer":
        auto_mode, auto_meta = choose_best_edit_mode(text, input_image=input_image, outfit_image=outfit_image, requested_mode="auto")
        print("🧭 V8 garment task auto-route:", json.dumps(auto_meta, ensure_ascii=False))
        if auto_mode == "garment":
            img, info = runners["garment_transfer"](
                input_image, outfit_image, text, style, resolution, progress
            )
            plan["mode_router"] = auto_meta
            return {
                "task": task,
                "plan": plan,
                "assistant_text": None,
                "image": img,
                "info": info,
            }
        # nếu router bảo text edit an toàn hơn thì chuyển luôn
        img, info = runners["flux_edit"](
            input_image, text, style, resolution, progress
        )
        plan["mode_router"] = auto_meta
        plan["task"] = "edit_clothes"
        plan["reason"] = "V8 auto-route: garment_transfer được hạ xuống text clothes edit vì reference không đủ tin cậy."
        return {
            "task": "edit_clothes",
            "plan": plan,
            "assistant_text": None,
            "image": img,
            "info": info,
        }

    # 5) Identity edit
    if task == "identity_edit":
        img, info = runners["identity_edit"](
            input_image, text, style, resolution, progress
        )
        return {
            "task": task,
            "plan": plan,
            "assistant_text": None,
            "image": img,
            "info": info,
        }

    # 6) Edit clothes / style / generic → ưu tiên Flux
    if task in {"edit_clothes", "style_edit", "generic_edit"}:
        if plan.get("use_flux", True):
            img, info = runners["flux_edit"](
                input_image, text, style, resolution, progress
            )
        else:
            img, info = runners["fallback_edit"](
                input_image, text, style, resolution, progress
            )

        return {
            "task": task,
            "plan": plan,
            "assistant_text": None,
            "image": img,
            "info": info,
        }

    # 7) fallback cuối
    img, info = runners["fallback_edit"](
        input_image, text, style, resolution, progress
    )
    return {
        "task": "fallback_edit",
        "plan": plan,
        "assistant_text": None,
        "image": img,
        "info": info,
    }

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
def _pick_best_quant(files: list) -> str:
    """
    Chọn quant TỐT NHẤT trong các file Kontext có sẵn.
    Ưu tiên vùng Q4–Q6 (cân bằng chất lượng/RAM cho máy 4GB + offload); Q3 chỉ khi không có gì hơn.
    Nhờ vậy: tải Q4_K_M vào models/unet là app TỰ dùng nó thay cho Q3_K_S cũ (không cần sửa code).
    """
    order = ["q6_k", "q5_k_m", "q5_k_s", "q5_1", "q5_0",
             "q4_k_m", "q4_k_s", "q4_1", "q4_0",
             "q3_k_l", "q3_k_m", "q3_k_s", "q2_k",
             "q8_0", "fp16", "bf16", "f16"]
    for kw in order:
        for f in files:
            if kw in f.lower().replace("-", "_"):
                return f
    return files[0]


# ── E + F: chọn model Kontext (quant) & LoRA thêm (style/trang phục) ──
_SELECTED_KONTEXT = {"unet": None}            # dropdown set; None = tự chọn quant tốt nhất
_EXTRA_LORA = {"name": None, "strength": 0.9} # LoRA Flux thêm, chồng lên Turbo
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
    """Nối Turbo LoRA + LoRA thêm (nếu có) vào input 'model' của KSampler node '50'. Sửa wf tại chỗ.
    CHẶN NẠP TRÙNG: nếu LoRA thêm trùng tên Turbo (không phân biệt hoa/thường) → bỏ qua,
    tránh chồng cùng 1 LoRA hai lần làm méo/đội RAM vô ích."""
    model_src = ["1", 0]
    nid = 10
    loaded = set()   # các lora_name (lower) đã nối → chặn trùng

    def _add(name, strength):
        nonlocal model_src, nid
        if not name:
            return None
        key = name.lower()
        if key in loaded:
            print(f"⏭️  Bỏ qua LoRA trùng: {name}")
            return None
        s = max(0.0, min(2.0, float(strength)))
        wf[str(nid)] = {"class_type": "LoraLoaderModelOnly",
                        "inputs": {"model": model_src, "lora_name": name,
                                   "strength_model": s}}
        model_src = [str(nid), 0]
        nid += 1
        loaded.add(key)
        return s

    if turbo_lora:
        _add(turbo_lora, lora_strength)
    extra = _EXTRA_LORA.get("name")
    if extra:
        es = _add(extra, _EXTRA_LORA.get("strength", 1.0))
        if es is not None:
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
    """
    Kiểm tra Flux Kontext có đủ model không.
    Trả về dict {unet, t5, clip_l, vae} nếu đủ, else {} (rỗng).
    """
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

    en = _ollama_chat(TRANSLATE_MODEL, system, vi_text,
                      temperature=0.2, num_predict=160, timeout=30, retries=2)
    if not en:
        return f"Edit the image based on: {vi_text}"
    en = en.split("\n")[0].replace('"', "").replace("`", "").strip()
    print(f"🧠 Kontext VI: {vi_text}")
    print(f"🧠 Kontext EN: {en}")
    return en if len(en) > 5 else f"Edit the image based on: {vi_text}"


def review_kontext_instruction(vi_text: str, en_candidate: str) -> str:
    """Vòng SOI LẠI: LLM tự kiểm tra bản dịch Kontext so với câu gốc tiếng Việt và sửa
    lỗi hay gặp (sai trang phục theo giới, gộp 2 nhân vật, rớt màu, dùng 'Transform',
    thiếu vế giữ mặt/tóc/dáng). 1 lượt soi. Trả về lệnh EN đã sửa (hoặc bản cũ nếu đã ổn / soi lỗi)."""
    if not en_candidate:
        return en_candidate
    system = """You review and FIX an English image-editing instruction for the Flux Kontext editor, comparing it against the original Vietnamese request.
Check and correct ALL of these:
- Clothing must match the request and the subject's gender. For a woman, "đồ bơi" = swimsuit/bikini/one-piece, NEVER "swim trunks".
- "mát mẻ" = revealing/skimpy; "kín đáo" = modest/conservative. Keep every stated color and material.
- If the Vietnamese gives DIFFERENT clothes to DIFFERENT people, the English MUST keep them separate, each named by a visible feature (hair/dress color), joined by ';'. NEVER merge into "both"/"them" when the items differ.
- Must start with Change/Replace/Dress/Add/Remove, NEVER "Transform".
- Unless the request changes the face/hair, it must end with a clause preserving identity (e.g. "while keeping the same faces, hairstyle, body and pose").
- Output ONE single-line instruction only. No quotes, no explanation, no preamble. If the candidate is already correct, output it unchanged."""
    user = (f"Vietnamese request: {vi_text}\n"
            f"Candidate English: {en_candidate}\n"
            "Return the corrected single-line instruction:")
    fixed = _ollama_chat(TRANSLATE_MODEL, system, user,
                         temperature=0.1, num_predict=180, timeout=35, retries=1)
    if not fixed:
        return en_candidate   # soi lỗi → giữ bản dịch gốc
    fixed = fixed.split("\n")[0].replace('"', "").replace("`", "").strip()
    for pre in ("corrected:", "instruction:", "output:", "english:"):
        if fixed.lower().startswith(pre):
            fixed = fixed[len(pre):].strip()
    if len(fixed) < 5:
        return en_candidate
    if fixed.strip().lower() != en_candidate.strip().lower():
        print(f"🔁 Soi lại Kontext → đã sửa: {fixed}")
    else:
        print("🔁 Soi lại Kontext → lệnh đã ổn")
    return fixed



def _pil_to_base64_png(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _get_ollama_vision_model() -> str:
    """Tìm model vision đang có trong Ollama. Ưu tiên các model phổ biến."""
    preferred = [
        "moondream", "llava:7b", "llava", "minicpm-v", "qwen2.5vl", "qwen2.5-vl", "qwen2-vl",
        "llama3.2-vision", "llava:13b", "bakllava", "granite3.2-vision"
    ]
    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        print(f"⚠️ Không đọc được model Ollama vision: {e}")
        return ""

    lower_map = {m.lower(): m for m in models}
    for pref in preferred:
        for k, original in lower_map.items():
            if pref in k:
                return original
    for k, original in lower_map.items():
        if any(tag in k for tag in ["vision", "llava", "moondream", "minicpm-v", "vl"]):
            return original
    return ""


def describe_outfit_image(outfit_image, note_vi="") -> tuple[str, str]:
    """
    Dùng model vision của Ollama để mô tả outfit trong ảnh tham chiếu.
    Trả về (english_description, model_name). Nếu không có model vision thì trả ("", "").
    """
    if outfit_image is None:
        return "", ""
    model = _get_ollama_vision_model()
    if not model:
        print("⚠️ Chưa có Ollama vision model → không thể caption outfit tự động.")
        return "", ""

    img = outfit_image.convert("RGB")
    max_side = 768
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))

    extra = (note_vi or "").strip()
    prompt = (
        "Describe ONLY the clothing/outfit shown in this reference image for use in image editing. "
        "Focus on garment types, colors, patterns, materials, accessories, and overall style. "
        "If there are multiple visible people or multiple outfits, describe each outfit separately using clear visual identifiers such as hair color or position. "
        "Do not describe the face unless it helps identify which outfit belongs to which person. "
        "Output concise English only, as one or two sentences."
    )
    if extra and extra.lower() not in {
        "mặc theo ảnh trang phục tham chiếu", "dùng ảnh trang phục tham chiếu", "mặc theo ảnh",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc, dáng người và tư thế",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc và tư thế",
    }:
        prompt += f" Extra user note: {extra}"

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [_pil_to_base64_png(img)],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 120},
    }
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            text = (r.json().get("response") or "").strip()
            text = text.replace("\n", " ").replace('"', '').strip()
            print(f"👁️ Outfit caption ({model}): {text}")
            return text, model
    except Exception as e:
        print(f"⚠️ Vision caption lỗi: {e}")
        return "", model


def describe_scene_image(scene_image, user_note="") -> tuple[str, str]:
    """V8.7: mô tả ảnh hiện tại để biết có mấy NHÂN VẬT người/anime, trái/phải, tóc, outfit hiện tại."""
    if scene_image is None:
        return "", ""
    model = _get_ollama_vision_model()
    if not model:
        return "", ""

    img = scene_image.convert("RGB")
    max_side = 640
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))

    prompt = (
        "Describe the visible HUMAN / ANIME-PERSON characters in this image for image editing. "
        "Count only person-like characters. Ignore pets, plush toys, dolls, printed figures, background art, and small hand-held creatures when counting characters. "
        "State how many human/anime-person characters are visible and identify them by position (left/right/center), "
        "hair color/style, current outfit, clearly attached outfit accessories, important pose/contact, and background. "
        "If there is a pet/plush/object being held, mention it as a held object, not as a character. "
        "Focus on helping an image editor know which character is which. "
        "Output concise English only, 1-4 short sentences."
    )
    note = (user_note or "").strip()
    if note:
        prompt += f" User request context: {note}"

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [_pil_to_base64_png(img)],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 160},
    }
    try:
        with httpx.Client(timeout=150) as c:
            r = c.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            text = (r.json().get("response") or "").strip()
            text = text.replace("\n", " ").replace('"', "").strip()
            print(f"🧭 Scene caption ({model}): {text}")
            return text, model
    except Exception as e:
        print(f"⚠️ Scene caption lỗi: {e}")
        return "", model

def _looks_like_clothes_request(text: str) -> bool:
    t = (text or "").lower()
    keys = [
        "đồ", "trang phục", "quần áo", "váy", "áo", "mặc", "sportswear", "outfit", "clothes", "dress",
        "shirt", "jacket", "uniform", "gym", "athletic", "thể thao", "cosplay", "đồng phục", "hoodie"
    ]
    return any(k in t for k in keys)


def _estimate_visible_character_count(scene_desc_en: str) -> int:
    t = " " + (scene_desc_en or "").lower().strip() + " "
    if not t.strip():
        return 0

    pet_terms = [
        " small animal", " animal-like", " plush", " stuffed", " toy", " doll", " figurine", " figure",
        " pet", " cat", " bunny", " rabbit", " puppy", " dog", " creature", " mascot"
    ]
    has_pet_like = any(k in t for k in pet_terms)

    # Strong positional cues are more reliable than raw number words.
    has_left = "character on the left" in t or "left character" in t
    has_right = "character on the right" in t or "right character" in t
    has_center = "character in the center" in t or "center character" in t

    if has_left and has_right:
        # In this project, many false '3 characters' detections come from a held plush/pet.
        if has_center and has_pet_like:
            return 2
        if re.search(r"(three|3)\s+(characters|people|girls|women)", t) and has_pet_like:
            return 2
        return 2 if not has_center else 3

    if re.search(r"(three|3)\s+(characters|people|girls|women)", t):
        return 2 if has_pet_like else 3

    if re.search(r"(two|2)\s+(characters|people|girls|women)", t):
        return 2

    if has_left or has_right or has_center:
        return 1

    if re.search(r"(one|1)\s+(character|person|girl|woman)", t):
        return 1

    return 1

def _extract_side_hints(scene_desc_en: str) -> dict:
    text = (scene_desc_en or "").strip()
    out = {"left": "", "right": ""}
    if not text:
        return out
    m_left = re.search(r"The character on the left([^.]*)\.", text, flags=re.I)
    m_right = re.search(r"The character on the right([^.]*)\.", text, flags=re.I)
    if m_left:
        out["left"] = ("The character on the left" + m_left.group(1)).strip()
    if m_right:
        out["right"] = ("The character on the right" + m_right.group(1)).strip()
    return out


def _build_multi_character_guard(scene_desc_en: str, scope: str = "all") -> str:
    scope = _normalize_scope(scope)
    n = _estimate_visible_character_count(scene_desc_en)
    if scope != "all" or n < 2:
        return ""
    sides = _extract_side_hints(scene_desc_en)
    parts = [
        f"There appear to be {n} visible characters in the image.",
        "This is a multi-character clothing edit.",
        "Apply the new target outfit to every visible character, not just the most prominent one.",
        "The left character must receive the clothing change.",
        "The right character must also receive the clothing change.",
        "Do not leave any visible character wearing their original clothes.",
    ]
    if sides.get("left"):
        parts.append(f"Left-side context: {sides['left']}.")
    if sides.get("right"):
        parts.append(f"Right-side context: {sides['right']}.")
    parts.append("Both left and right characters should clearly show the new outfit after editing.")
    return " ".join(parts)




def _user_explicitly_requested_cleanup(text: str = "") -> bool:
    """V8.5: chỉ cleanup phụ kiện/prop khi user nói rõ muốn xóa/bỏ/remove."""
    t = (text or "").lower()
    keys = [
        "xóa", "xoá", "bỏ", "loại bỏ", "gỡ", "remove", "delete", "erase",
        "bỏ tai", "xóa tai", "xóa mèo", "bỏ mèo", "xóa thú", "bỏ thú",
        "remove cat", "remove ears", "remove plush", "remove prop", "remove accessory"
    ]
    return any(k in t for k in keys)


def _build_character_count_lock_instruction(expected_count: int = 0) -> str:
    if expected_count and expected_count >= 2:
        return (
            f"Keep exactly {expected_count} visible characters as in the input image. "
            "Do not remove, hide, crop out, merge, replace, or turn any visible character into a prop/background element. "
            "The outfit reference is for clothing style only, not for the number of people, pose, composition, or scene layout. "
            "Only modify clothing regions; preserve both visible characters, their faces, bodies, hands, pose, and overall composition."
        )
    return (
        "Keep exactly the same number of visible characters as in the input image. "
        "Do not remove, hide, crop out, merge, or replace any visible character. "
        "Use the outfit reference for clothing style only."
    )


def verify_character_count_lock(result_image, expected_count: int, user_request_vi: str = "") -> dict:
    """V8.5 hard lock: nếu output làm mất nhân vật thì reject output và giữ ảnh cũ."""
    expected_count = int(expected_count or 0)
    if expected_count < 2 or result_image is None:
        return {"ok": True, "expected": expected_count, "found": 0, "reason": "not required"}
    try:
        out_scene, model = describe_scene_image(result_image, user_request_vi)
        found = _estimate_visible_character_count(out_scene)
        ok = (found == 0) or (found >= expected_count)
        reason = f"output_scene({model}): {out_scene}" if out_scene else "output scene unknown"
        print(f"🔒 Character count lock: expected={expected_count}, found={found}, ok={ok}")
        return {"ok": ok, "expected": expected_count, "found": found, "reason": reason, "scene": out_scene}
    except Exception as e:
        print(f"⚠️ Character count lock lỗi: {e}")
        return {"ok": True, "expected": expected_count, "found": 0, "reason": f"lock error ignored: {e}"}


def infer_source_only_remove_list(scene_desc_en: str = "", outfit_caption_en: str = "", user_request_vi: str = "") -> list[str]:
    """V8.5: mặc định KHÔNG cleanup phụ kiện/prop; chỉ suy ra remove_list khi user yêu cầu rõ."""
    if not _user_explicitly_requested_cleanup(user_request_vi):
        return []
    scene = (scene_desc_en or "").lower()
    outfit = (outfit_caption_en or "").lower()
    items = []

    def scene_has(*keys):
        return any(k in scene for k in keys)

    def outfit_has(*keys):
        return any(k in outfit for k in keys)

    if scene_has("cat ear", "cat ears", "animal ear", "animal ears", "cat-like ear", "cat-like ears") and not outfit_has("cat ear", "cat ears", "animal ear", "animal ears"):
        items.append("cat ears / animal-ear head accessories")

    if scene_has("small black cat", "black cat", "holding a cat", "cat plush", "plush", "stuffed", "toy", "bunny") and not outfit_has("cat", "plush", "stuffed", "toy", "bunny"):
        items.append("cat/plush toy or hand-held animal prop")

    if scene_has("maid", "maid outfit", "maid-style", "maid collar", "black and white outfit") and not outfit_has("maid"):
        items.append("maid-style outfit remnants and maid accessories")

    if scene_has("collar", "cat-like collar", "black and white collar") and not outfit_has("collar"):
        items.append("old collar/choker/accessory around the neck")

    # Deduplicate but keep order
    dedup = []
    for it in items:
        if it not in dedup:
            dedup.append(it)
    return dedup


def build_source_only_cleanup_instruction(remove_list: list[str], scope: str = "all") -> str:
    """V8.5: cleanup cực kỳ an toàn; không được xóa người/bố cục."""
    remove_list = [str(x).strip() for x in (remove_list or []) if str(x).strip()]
    if not remove_list:
        return ""
    scope = _normalize_scope(scope)
    target = {
        "left": "only on the left-side character",
        "right": "only on the right-side character",
        "all": "on visible characters only where clearly applicable",
        "auto": "on the intended visible character",
    }.get(scope, "on visible characters only where clearly applicable")
    items = ", ".join(remove_list)
    return (
        f"Optional source-only cleanup requested by user: remove only these items: {items} {target}, only if they are clearly separate from the character body and not part of the target outfit reference. "
        "Do not remove, hide, crop, merge, or replace any visible character. Do not remove faces, bodies, arms, hands, or the number of people. "
        "If removing the item risks deleting a character or damaging pose/composition, keep the item instead. "
        "The priority is preserving all visible characters and only changing clothing."
    )


def _wants_reference_outfit(text: str) -> bool:
    t = (text or "").lower().strip()
    keys = [
        "mặc theo ảnh", "mặc theo ảnh trang phục", "dùng ảnh trang phục", "use the outfit reference",
        "use the clothing reference", "same outfit as the reference", "copy the outfit from the reference",
        "match the outfit from the reference", "the exact outfit from the clothing reference"
    ]
    return any(k in t for k in keys)


def choose_best_edit_mode(user_prompt: str, input_image=None, outfit_image=None, requested_mode: str = "auto") -> tuple[str, dict]:
    """V8: tự chọn mode phù hợp hơn để tránh route nhầm garment/reference."""
    req = (requested_mode or "auto").lower().strip()
    note = (user_prompt or "").strip()
    has_input = input_image is not None
    has_outfit = outfit_image is not None
    same_reference = _images_maybe_same(input_image, outfit_image) if (has_input and has_outfit) else False
    wants_ref = _wants_reference_outfit(note)
    clothes_req = _looks_like_clothes_request(note)

    scene_hint = ""
    n_chars = 0
    if has_input:
        try:
            scene_hint, _ = describe_scene_image(input_image, note)
            n_chars = _estimate_visible_character_count(scene_hint)
        except Exception:
            scene_hint = ""
            n_chars = 0

    meta = {
        "requested_mode": req,
        "has_input": has_input,
        "has_outfit": has_outfit,
        "same_reference": same_reference,
        "wants_reference": wants_ref,
        "clothes_request": clothes_req,
        "visible_characters": n_chars,
    }

    if req == "region":
        meta["decision"] = "region"
        return "region", meta

    if req == "garment":
        if has_outfit and not same_reference:
            meta["decision"] = "garment"
            return "garment", meta
        meta["decision"] = "flux_edit"
        meta["reason"] = "explicit garment mode but outfit reference is missing or too similar to current image"
        return "flux_edit", meta

    # auto mode
    if has_input and clothes_req:
        if has_outfit and wants_ref and not same_reference:
            meta["decision"] = "garment"
            meta["reason"] = "has outfit reference + user explicitly wants reference outfit"
            return "garment", meta
        meta["decision"] = "flux_edit"
        meta["reason"] = "clothes edit from text only (safer than garment reference)"
        return "flux_edit", meta

    meta["decision"] = req if req != "auto" else "planner"
    return meta["decision"], meta


def build_teacher_plan_json(user_request_vi: str, scene_desc_en: str = "", outfit_caption_en: str = "", scope: str = "all") -> dict:
    """V7: tạo plan JSON trung gian trước khi compile thành prompt cho Flux."""
    scope = _normalize_scope(scope)
    request_vi = (user_request_vi or "").strip()
    req_en = ""
    if request_vi:
        try:
            req_en = translate_for_kontext(request_vi)
            # V7.1: KHÔNG soi lại/rewrite ở teacher-plan để tránh hallucinate thêm yêu cầu ngoài ý user
        except Exception as e:
            print(f"⚠️ Không dịch được request cho teacher plan: {e}")
            req_en = request_vi

    plan = {
        "version": "teacher_v8_7",
        "task": "edit_clothes" if _looks_like_clothes_request(request_vi) else "image_edit",
        "scope": scope,
        "user_request_vi": request_vi,
        "user_request_en": req_en,
        "scene_understanding": scene_desc_en,
        "outfit_reference": outfit_caption_en,
        "source_only_remove_list": infer_source_only_remove_list(scene_desc_en, outfit_caption_en, user_request_vi),
        "target_rule": _scope_instruction_en(scope),
        "preserve": ["identity", "face", "general hairstyle", "body shape", "hand placement", "pose", "background", "composition"],
        "forbidden": ["do not change face", "do not change hairstyle drastically", "do not change background unless requested", "do not preserve source-only outfit accessories or props if they conflict with the target outfit"],
        "retry_policy": {
            "max_retry": 1,
            "verify_with_vision": True,
        }
    }
    return plan


def compile_teacher_instruction(plan: dict) -> str:
    """V8.7: compile plan thành instruction EN ngắn hơn, rõ side-lock hơn, tránh prompt quá dài/đá nhau."""
    scope = _normalize_scope(plan.get("scope") or "all")
    scope_rule = plan.get("target_rule") or _scope_instruction_en(scope)
    scene = (plan.get("scene_understanding") or "").strip()
    req_en = (plan.get("user_request_en") or plan.get("user_request_vi") or "").strip()
    outfit_ref = (plan.get("outfit_reference") or "").strip()
    remove_list = plan.get("source_only_remove_list") or []
    n_chars = _estimate_visible_character_count(scene)
    sides = _extract_side_hints(scene)

    if outfit_ref:
        core = (
            f"Apply this target outfit to the target character(s): {outfit_ref}. "
            f"Match the clothing type, colors, pattern, material, silhouette, and attached outfit accessories as closely as possible"
        )
    elif req_en:
        core = f"Edit the clothing according to this request: {req_en}"
    else:
        core = "Edit the clothing according to the user's request"

    scene_hint = f"Scene understanding: {scene}. " if scene else ""
    preserve = (
        "Preserve each visible human/anime character's identity, face, general hairstyle, body, arms, hands, pose, relative placement, background, and overall composition"
    )
    count_guard = (
        "Count only human/anime-person characters. Ignore pets, plush toys, dolls, and held objects when counting characters. "
        "Keep exactly the same number of visible human/anime characters as in the input image. Do not remove, hide, merge, crop out, or replace any visible character."
    )
    reference_guard = (
        "Use the outfit reference only as a clothing-style reference. Do not copy its number of people, pose, background, scene layout, or props."
    )
    prop_guard = (
        "Only change clothing and outfit-attached accessories. Do not accidentally edit only a hand or a tiny fragment. "
        "Do not transform a visible character into a prop/background element."
    )

    cleanup_items = ", ".join(str(x).strip() for x in remove_list if str(x).strip())
    if cleanup_items:
        cleanup_guard = (
            f"If clearly visible and not part of the target outfit, remove only these old outfit-specific remnants: {cleanup_items}. "
            "Do not remove separate hand-held objects or pets unless the user explicitly asked to remove them."
        )
    else:
        cleanup_guard = (
            "Do not preserve old maid/headpiece/collar/animal-ear outfit accessories if they conflict with the target outfit. "
            "Do not remove separate hand-held objects or pets unless the user explicitly asked to remove them."
        )

    multi_guard = ""
    side_guard = ""
    if scope == "all":
        if n_chars >= 2:
            left_desc = sides.get("left", "")
            right_desc = sides.get("right", "")
            multi_guard = (
                f"This is a multi-character clothing edit. There are {n_chars} visible human/anime characters and every one of them must receive the clothing change. "
                "The left visible character must change clothes. The right visible character must also change clothes. "
                "Do not leave any visible character wearing the original outfit."
            )
            if left_desc:
                multi_guard += f" Left-side anchor: {left_desc}."
            if right_desc:
                multi_guard += f" Right-side anchor: {right_desc}."
    elif scope in {"left", "right"}:
        other = "right" if scope == "left" else "left"
        target_desc = (sides.get(scope) or "").strip()
        other_desc = (sides.get(other) or "").strip()
        side_guard = (
            f"This pass targets only the {scope}-side visible character. Fully replace the {scope}-side character's entire visible outfit. "
            f"Keep the {other}-side visible character unchanged in this pass except unavoidable shared-contact continuity. "
            "Do not swap outfits between characters."
        )
        if target_desc:
            side_guard += f" Target-side anchor: {target_desc}."
        if other_desc:
            side_guard += f" Non-target-side anchor: {other_desc}."

    parts = [scene_hint, f"{scope_rule}.", core + ".", preserve + ".", count_guard, reference_guard, prop_guard, cleanup_guard]
    if multi_guard:
        parts.append(multi_guard)
    if side_guard:
        parts.append(side_guard)
    return " ".join(p.strip() for p in parts if p and p.strip()).strip()

def verify_image_edit_result(result_image, user_request_vi: str, scope: str = "all", outfit_caption_en: str = "", scene_desc_en: str = "", original_image=None) -> dict:
    """V8.7: dùng vision model kiểm tra output có đạt lệnh không; count chỉ tính human/anime characters."""
    model = _get_ollama_vision_model()
    if not model or result_image is None:
        return {"success": True, "score": 0.0, "reason": "no vision verifier", "retry_instruction": ""}

    scope = _normalize_scope(scope)
    img = result_image.convert("RGB")
    max_side = 640
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))

    remove_list = infer_source_only_remove_list(scene_desc_en, outfit_caption_en, user_request_vi)

    # V8.11: verifier phải hiểu rõ đây là PASS theo scope nào.
    # Trước đó scope=left vẫn bị chấm theo mục tiêu all, nên pass trái đúng cũng bị reject vì nhân vật phải chưa đổi.
    if scope == "left":
        scope_rules = """
CURRENT CHECK MODE: LEFT-SIDE PASS ONLY, not the final all-character check.
Success is true when:
- the left visible human/anime character's clothing changed to the target outfit/reference,
- the right visible human/anime character is still visible and mostly unchanged,
- the character count stayed the same.
It is OK and expected that the right character may still wear the original/old outfit in this left-side pass.
Fail only if the left character did not receive the target clothing, if the right character was wrongly changed/removed, or if character count changed.
The retry_instruction must target ONLY the left character. Do not instruct editing the right character.
""".strip()
    elif scope == "right":
        scope_rules = """
CURRENT CHECK MODE: RIGHT-SIDE PASS ONLY, not the final all-character check.
Success is true when:
- the right visible human/anime character's clothing changed to the target outfit/reference,
- the left visible human/anime character is still visible and mostly unchanged,
- the character count stayed the same.
It is OK and expected that the left character may already wear the target outfit from a previous pass.
Fail only if the right character did not receive the target clothing, if the left character was wrongly changed/removed, or if character count changed.
The retry_instruction must target ONLY the right character. Do not instruct editing the left character.
""".strip()
    else:
        scope_rules = """
CURRENT CHECK MODE: FINAL / ALL-CHARACTERS CHECK.
Success is true only if every visible human/anime character has the requested clothing change and no visible character still mostly wears the old outfit.
The retry_instruction may mention the missing side(s), but must not ask to remove, hide, merge, crop out, or replace any visible character.
""".strip()

    prompt = f"""
You are a strict image-edit verifier.
User request: {user_request_vi}
Scope: {scope}
Expected outfit/reference: {outfit_caption_en or 'based on the user request'}
Original scene understanding: {scene_desc_en}
Source-only old outfit remnants that may need removal if visible and not in the target outfit: {', '.join(remove_list) if remove_list else 'none inferred'}

{scope_rules}

Check whether the edited image satisfies the CURRENT CHECK MODE above, not just the global user request.
Count only HUMAN / ANIME-PERSON characters. Ignore pets, plush toys, dolls, printed figures, and hand-held objects when counting characters.
The edited result must keep exactly the same number of visible human/anime characters as the original input. If any visible character disappears, is cropped out, merged away, replaced, or hidden, success must be false.
Be strict about multi-character edits. Check the left visible character and the right visible character separately when applicable.
Old outfit remnants should be removed only on the target side(s). For a non-target side in a left/right pass, old clothing/accessories may remain because that side is intentionally preserved.
Separate hand-held props/pets should not be treated as characters.
If even one visible target character still mostly wears the old outfit, success must be false.
Return JSON only with keys:
{{"success": true/false, "score": 0-100, "reason": "short", "retry_instruction": "short correction instruction in English"}}
""".strip()

    images_payload = []
    if original_image is not None:
        try:
            orig = original_image.convert("RGB")
            ow, oh = orig.size
            oscale = min(max_side / max(ow, oh), 1.0)
            if oscale < 1.0:
                orig = orig.resize((int(ow * oscale), int(oh * oscale)))
            images_payload.append(_pil_to_base64_png(orig))
        except Exception as e:
            print(f"⚠️ Verify prepare original_image lỗi: {e}")
    images_payload.append(_pil_to_base64_png(img))

    prompt += "\nTreat the first image as the original input (if provided) and the last image as the edited result. Compare them carefully."

    payload = {
        "model": model,
        "prompt": prompt,
        "images": images_payload,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 220},
    }
    try:
        with httpx.Client(timeout=150) as c:
            r = c.post(f"{OLLAMA_URL}/api/generate", json=payload)
            r.raise_for_status()
            raw = (r.json().get("response") or "").strip()
        raw_clean = raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r"\{.*\}", raw_clean, flags=re.S)
        data = json.loads(m.group(0) if m else raw_clean)
        data["success"] = bool(data.get("success"))
        data["score"] = float(data.get("score", 0))
        data["reason"] = str(data.get("reason", ""))
        data["retry_instruction"] = str(data.get("retry_instruction", ""))
        print(f"🧪 Verify ({model}): {data}")
        return data
    except Exception as e:
        print(f"⚠️ Verify lỗi: {e}")
        return {"success": False, "score": 0.0, "reason": f"verifier error: {e}", "retry_instruction": "Retry with strict multi-character lock: keep both visible human/anime characters, change missing clothes only, and ignore plush/pets when counting characters."}


def _side_scope_reason_accepts(verify: dict, scope: str) -> bool:
    """V8.11: chấp nhận side-pass khi verifier mô tả đúng target đã đổi nhưng lại fail vì non-target chưa đổi.
    Đây là lớp an toàn phụ cho trường hợp vision model vẫn hiểu nhầm scope=left/right là scope=all.
    """
    scope = _normalize_scope(scope)
    if scope not in {"left", "right"}:
        return False
    reason = str((verify or {}).get("reason", "") or "").lower()
    if not reason:
        return False

    bad = ("character count", "disappear", "disappeared", "removed", "cropped", "merged", "hidden", "missing character", "wrongly changed")
    if any(x in reason for x in bad):
        return False

    other = "right" if scope == "left" else "left"
    target_changed = (
        scope in reason and (
            "changed" in reason or "has been changed" in reason or "wearing" in reason or "outfit has been" in reason
        )
    )
    other_preserved = (
        other in reason and (
            "unchanged" in reason or "remains" in reason or "still" in reason or "old" in reason
        )
    )
    return bool(target_changed and other_preserved)


def _sanitize_retry_instruction_for_scope(retry_inst: str, scope: str) -> str:
    """V8.11: retry_instruction của verifier không được kéo nhầm sang nhân vật ngoài scope."""
    scope = _normalize_scope(scope)
    txt = (retry_inst or "").strip()
    low = txt.lower()
    if scope == "left":
        banned = ("right character", "right-side", "right visible", "both characters", "every visible", "all visible", "all characters")
        if not txt or any(b in low for b in banned):
            return "Complete only the left visible character's outfit change to match the target outfit/reference. Keep the right visible character unchanged."
    elif scope == "right":
        banned = ("left character", "left-side", "left visible", "both characters", "every visible", "all visible", "all characters")
        if not txt or any(b in low for b in banned):
            return "Complete only the right visible character's outfit change to match the target outfit/reference. Keep the left visible character unchanged."
    return txt

def run_flux_teacher_v7(input_image, user_request_vi: str, instruction_en: str, *, progress=None, guidance=4.8, steps=32, lora_strength=1.0, scope="all", outfit_caption_en="", scene_desc_en="", auto_verify=True):
    """V8.10 executor: safe-commit.
    Chỉ commit ảnh mới khi pass đó vượt count-lock + verify; nếu fail thì rollback về input_image."""
    guidance = max(4.2, min(5.8, float(guidance)))
    if YUMEAI_LOW_VRAM_MODE:
        steps = _resource_safe_steps(steps, kind="teacher")
    else:
        steps = max(28, min(40, int(steps)))

    def _verify_ok(v: dict) -> bool:
        try:
            if bool(v.get("success")) and float(v.get("score", 0)) >= 70.0:
                return True
            if _side_scope_reason_accepts(v, scope):
                v["success"] = True
                v["score"] = max(float(v.get("score", 0) or 0), 75.0)
                v["reason"] = str(v.get("reason", "")) + " | V8.11 side-pass accepted: target side changed and non-target side preserved."
                v["retry_instruction"] = ""
                return True
            return False
        except Exception:
            return False

    img, info = flux_kontext_fn(
        input_image=input_image,
        instruction_vi=user_request_vi,
        progress=progress,
        guidance=guidance,
        steps=steps,
        use_turbo=False,
        lora_strength=lora_strength,
        instruction_override=instruction_en,
        review=False,
    )

    expected_count = _estimate_visible_character_count(scene_desc_en)
    count_lock = verify_character_count_lock(img, expected_count, user_request_vi)
    if expected_count >= 2 and not count_lock.get("ok", True):
        reject_info = (
            "🚫 V8.10 SAFE-COMMIT REJECT — character count changed\n"
            f"🔒 count_lock: {count_lock}\n"
            f"📝 rejected_instruction: {instruction_en}\n"
            "↩️ rollback_to: previous committed image\n"
            "Giữ lại ảnh trước đó để tránh update state bằng ảnh lỗi.\n"
        )
        return input_image, reject_info

    verify = {"success": True, "score": 100, "reason": "verification disabled", "retry_instruction": ""}
    if auto_verify:
        if progress:
            progress(0.96, desc="🧪 Đang kiểm tra ảnh kết quả...")
        verify = verify_image_edit_result(
            img,
            user_request_vi,
            scope=scope,
            outfit_caption_en=outfit_caption_en,
            scene_desc_en=scene_desc_en,
            original_image=input_image,
        )

    verify_reason = str(verify.get("reason", "") or "")
    verify_failed = not _verify_ok(verify)
    verify_timeout = ("timed out" in verify_reason.lower()) or ("verifier error" in verify_reason.lower())
    scope_n = _normalize_scope(scope)
    multi_char = _estimate_visible_character_count(scene_desc_en) >= 2 and scope_n == "all"

    if auto_verify and (verify_failed or (multi_char and verify_timeout)):
        if YUMEAI_TEACHER_RETRY_MAX <= 0:
            reject_info = (
                "🚫 V8.12 SAFE-COMMIT REJECT — verify chưa đạt, không retry tự động vì đang bật Low-VRAM mode\n"
                f"🧪 verify: {verify}\n"
                f"📝 rejected_instruction: {instruction_en}\n"
                "↩️ rollback_to: previous committed image\n"
                "🛡️ Đã bỏ retry để tránh chạy thêm job Flux rất lâu làm tràn VRAM/RAM. "
                "Muốn bật lại 1 retry: đặt $env:YUMEAI_TEACHER_RETRY_MAX=\"1\" rồi chạy lại app.\n"
            )
            return input_image, reject_info
        retry_inst = _sanitize_retry_instruction_for_scope((verify.get("retry_instruction") or "").strip(), scope_n)
        retry_remove_list = infer_source_only_remove_list(scene_desc_en, outfit_caption_en, user_request_vi)
        if scope_n == "left":
            scope_fix = "This retry targets only the left visible character. Fully change the left character's outfit to match the target outfit. Keep the right visible character unchanged in this retry. Do not accidentally change only the hands or a small region."
            cleanup = build_source_only_cleanup_instruction(retry_remove_list, "left") or "Do not remove props, pets, animal ears, or accessories unless explicitly requested by the user; only complete the clothing change for the left character."
        elif scope_n == "right":
            scope_fix = "This retry targets only the right visible character. Fully change the right character's outfit to match the target outfit. Keep the left visible character unchanged in this retry. Do not accidentally change only the hands or a small region."
            cleanup = build_source_only_cleanup_instruction(retry_remove_list, "right") or "Do not remove props, pets, animal ears, or accessories unless explicitly requested by the user; only complete the clothing change for the right character."
        else:
            scope_fix = "This is a multi-character clothing edit. Ensure the left visible character changes clothes and the right visible character also changes clothes. Do not leave any visible character wearing the old outfit."
            cleanup = build_source_only_cleanup_instruction(retry_remove_list, "all") or "Do not remove props, pets, animal ears, accessories, or visible characters unless explicitly requested by the user; only complete missing clothing changes."

        stronger = (
            instruction_en + " Correction pass: " + retry_inst + " " + scope_fix + " " + cleanup +
            " Make the requested clothing change more explicit and complete."
        )
        print(f"🔁 V8.10 retry vì verifier chưa đạt: {verify}")
        img2, info2 = flux_kontext_fn(
            input_image=input_image,
            instruction_vi=user_request_vi,
            progress=progress,
            guidance=min(5.8, guidance + 0.4),
            steps=_resource_safe_steps(max(36, steps), kind="teacher"),
            use_turbo=False,
            lora_strength=lora_strength,
            instruction_override=stronger,
            review=False,
        )
        count_lock2 = verify_character_count_lock(img2, expected_count, user_request_vi)
        if expected_count >= 2 and not count_lock2.get("ok", True):
            reject_info = (
                "🚫 V8.10 SAFE-COMMIT REJECT — retry changed character count\n"
                f"🔒 count_lock: {count_lock2}\n"
                f"🧪 verify1: {verify}\n"
                "↩️ rollback_to: previous committed image\n"
                "Giữ lại ảnh trước đó để tránh mất nhân vật.\n"
            )
            return input_image, reject_info
        verify2 = verify_image_edit_result(
            img2,
            user_request_vi,
            scope=scope,
            outfit_caption_en=outfit_caption_en,
            scene_desc_en=scene_desc_en,
            original_image=input_image,
        ) if auto_verify else verify
        if not _verify_ok(verify2):
            reject_info = (
                "🚫 V8.10 SAFE-COMMIT REJECT — retry verify chưa đạt\n"
                f"🧪 verify1: {verify}\n"
                f"🧪 verify2: {verify2}\n"
                f"📝 rejected_retry_instruction: {stronger}\n"
                "↩️ rollback_to: previous committed image\n"
                "Không commit ảnh retry vì verifier chưa xác nhận đạt.\n"
            )
            return input_image, reject_info
        info2 = (
            "🧠 V8.10 Teacher Pipeline — RETRY COMMIT OK\n"
            f"📝 {stronger}\n"
            f"🧪 verify1: {verify}\n"
            f"🧪 verify2: {verify2}\n" + "\n".join(info2.split("\n")[1:])
        )
        return img2, info2

    info = (
        "🧠 V8.10 Teacher Pipeline — COMMIT OK\n"
        f"📝 {instruction_en}\n"
        f"🧪 verify: {verify}\n" + "\n".join(info.split("\n")[1:])
    )
    return img, info

def build_outfit_edit_instruction_from_caption(outfit_caption_en: str, note_vi: str = "", scope: str = "all") -> str:
    """Tạo instruction EN mạnh để Flux Kontext thay đồ theo caption outfit, có phân biệt nhân vật trái/phải/tất cả."""
    scope_en = _scope_instruction_en(scope)
    if outfit_caption_en:
        base = (
            f"{scope_en}. Replace the current clothes with this outfit: {outfit_caption_en}. "
            f"Match the clothing design, colors, patterns, materials, accessories and overall style as closely as possible"
        )
    else:
        base = f"{scope_en}. Replace the current clothes with the requested new outfit"

    keep = "while keeping the same face, hairstyle, body shape, pose, background and composition unchanged"

    extra = ""
    generic_notes = {
        "mặc theo ảnh trang phục tham chiếu", "dùng ảnh trang phục tham chiếu", "mặc theo ảnh",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc, dáng người và tư thế",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc và tư thế",
    }
    note = (note_vi or "").strip()
    if note and note.lower() not in generic_notes:
        extra_en = translate_for_kontext(note)
        extra_en = review_kontext_instruction(note, extra_en)
        if extra_en:
            extra = f". Additional requirement: {extra_en}"

    strict_all = ""
    if _normalize_scope(scope) == "all":
        strict_all = ". Important: every visible character must be changed; do not leave any visible character wearing the original clothes"

    return f"{base}, {keep}{strict_all}{extra}"


def flux_kontext_fn(input_image, instruction_vi, progress=None,
                    guidance=3.5, steps=28, use_turbo=True, lora_strength=1.0,
                    instruction_override="", review=True):
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
        steps = _resource_safe_steps(steps, kind="flux")
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
        if review:
            instruction_en = review_kontext_instruction(instruction_vi, instruction_en)

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

    # Nối Turbo LoRA + LoRA thêm (nếu có) vào KSampler
    _chain_loras_into(wf, turbo_lora, lora_strength)

    if progress: progress(0.15, desc="Đang chạy Flux Kontext (rất chậm trên 4GB)...")
    pid = submit_workflow(wf)
    # Flux trên 4GB cực chậm → timeout dài (mặc định 30 phút, chỉnh bằng YUMEAI_COMFY_TIMEOUT_SEC)
    img = poll_and_download(pid, progress=progress, start_pct=0.15, timeout=COMFY_LONG_TIMEOUT_SEC)

    info = (f"🧠 Flux Kontext (edit thông minh)\n📝 {instruction_en}\n"
            f"🎚️ guidance:{guidance} | steps:{steps} | 🎲 seed:{seed}")
    return img, info


# ══════════════════════════════════
# UNIFIED CHAT HANDLER
# ══════════════════════════════════
def preview_kontext_fn(message, review=True):
    """Dịch thử lệnh VI → EN cho Kontext (KHÔNG tạo ảnh). Nếu bật review → soi lại luôn,
    để ô EN khớp đúng lệnh sẽ chạy thật (vì khi ô EN có chữ, lúc Gửi sẽ dùng thẳng, bỏ qua review)."""
    text = (message or "").strip()
    if not text:
        return gr.update()   # không có gì để dịch → giữ nguyên ô EN
    en = translate_for_kontext(text)
    if review:
        en = review_kontext_instruction(text, en)
    return en


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


# ── Method C: đổi đồ theo VÙNG (mask inpaint) cho TỪNG nhân vật ──
_VI_COLORS = {
    "xanh dương": "blue", "xanh lá": "green", "xanh lam": "blue", "xanh": "blue",
    "đỏ": "red", "cam": "orange", "vàng": "yellow", "tím": "purple", "hồng": "pink",
    "đen": "black", "trắng": "white", "xám": "gray", "nâu": "brown", "be": "beige",
}
_VI_GARMENTS = {
    "áo khoác": "jacket", "áo thun": "t-shirt", "áo tắm": "swimsuit", "áo": "shirt. top",
    "quần short": "shorts", "quần đùi": "shorts", "quần": "pants",
    "váy": "dress", "đầm": "dress", "bikini": "bikini", "đồ bơi": "swimsuit", "đồ": "outfit",
}


def translate_garment_region(vi_text: str) -> str:
    """Dịch 'món đồ + màu' để GroundingDINO khoanh đúng vùng của 1 nhân vật (GIỮ màu để phân biệt)."""
    t = vi_text.lower().strip()
    color   = next((en for vi, en in sorted(_VI_COLORS.items(),   key=lambda x: -len(x[0])) if vi in t), "")
    garment = next((en for vi, en in sorted(_VI_GARMENTS.items(), key=lambda x: -len(x[0])) if vi in t), "")
    if garment:
        return (color + " " + garment).strip()
    return translate_vi_en(vi_text)


def region_edit_fn(input_image, target_vi, new_outfit_vi, style, resolution, progress=None):
    """
    Đổi đồ THEO VÙNG (mask): GroundingDINO+SAM khoanh đúng món đồ (theo màu để trúng 1 người)
    → SD inpaint vẽ đồ mới CHỈ trong vùng đó. Mặt/tóc/người kia nằm ngoài mask nên giữ nguyên.
    Chạy 1 lần cho mỗi nhân vật. Không cần bật Flux Kontext.
    """
    if input_image is None:
        raise gr.Error("Chưa có ảnh tham chiếu!")
    if not (target_vi or "").strip() or not (new_outfit_vi or "").strip():
        raise gr.Error("Cần nhập cả 'vùng cần đổi' và 'đồ mới'!")
    if not get_segment_available():
        raise gr.Error("Chưa cài segment-anything (cài comfyui_segment_anything rồi restart ComfyUI).")

    target_en = translate_garment_region(target_vi)
    outfit_en = translate_vi_en(new_outfit_vi)
    print(f"🎯 Region: '{target_vi}' → mask '{target_en}'  |  đồ mới '{new_outfit_vi}' → '{outfit_en}'")

    if progress: progress(0.1, desc="Upload ảnh...")
    img = input_image.convert("RGB")
    w, h = img.size
    target = int(resolution)
    scale = min(target / w, target / h)
    nw = max(64, (int(w * scale) // 64) * 64)
    nh = max(64, (int(h * scale) // 64) * 64)
    img = img.resize((nw, nh))
    uploaded = upload_image_to_comfy(img)
    seed = random.randint(1, 2**31)

    model = get_inpaint_model(style if style in ("Anime", "Realistic", "Semi") else "Anime")
    if not model:
        raise gr.Error("Không tìm thấy checkpoint inpaint!")
    print(f"🎨 Region inpaint model: {model}")

    style_tag = {"Anime": "anime style", "Realistic": "photorealistic",
                 "Semi": "semi-realistic digital art"}.get(style, "anime style")
    full_prompt = (f"{outfit_en}, {style_tag}, same person, natural body, "
                   "seamless, highly detailed, best quality")
    neg_prompt = ("worst quality, low quality, blurry, deformed, extra limbs, bad anatomy, "
                  "different person, changed face, watermark, text")

    wf = {
        "20": {"class_type": "LoadImage", "inputs": {"image": uploaded}},
        "30": {"class_type": "GroundingDinoModelLoader (segment anything)",
               "inputs": {"model_name": "GroundingDINO_SwinT_OGC (694MB)"}},
        "31": {"class_type": "SAMModelLoader (segment anything)",
               "inputs": {"model_name": "sam_vit_b (375MB)"}},
        "32": {"class_type": "GroundingDinoSAMSegment (segment anything)",
               "inputs": {"grounding_dino_model": ["30", 0], "sam_model": ["31", 0],
                          "image": ["20", 0], "prompt": target_en, "threshold": 0.3}},
        "33": {"class_type": "GrowMask",
               "inputs": {"mask": ["32", 1], "expand": 10, "tapered_corners": True}},
        "34": {"class_type": "FeatherMask",
               "inputs": {"mask": ["33", 0], "left": 8, "top": 8, "right": 8, "bottom": 8}},
        "1":  {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "40": {"class_type": "CLIPTextEncode", "inputs": {"text": full_prompt, "clip": ["1", 1]}},
        "41": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_prompt, "clip": ["1", 1]}},
        "42": {"class_type": "VAEEncodeForInpaint",
               "inputs": {"pixels": ["20", 0], "vae": ["1", 2], "mask": ["34", 0], "grow_mask_by": 6}},
        "50": {"class_type": "KSampler",
               "inputs": {"model": ["1", 0], "positive": ["40", 0], "negative": ["41", 0],
                          "latent_image": ["42", 0], "seed": seed, "steps": 30, "cfg": 7.0,
                          "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0}},
        "60": {"class_type": "VAEDecode", "inputs": {"samples": ["50", 0], "vae": ["1", 2]}},
        "70": {"class_type": "SaveImage", "inputs": {"images": ["60", 0], "filename_prefix": "yumeai_region"}},
    }
    if progress: progress(0.3, desc=f"Đổi đồ vùng '{target_en}'...")
    pid = submit_workflow(wf)
    result = poll_and_download(pid, progress=progress, start_pct=0.3, timeout=300)
    info = (f"🎯 Đổi đồ vùng: '{target_vi}' → '{new_outfit_vi}'\n"
            f"📐 {nw}x{nh} | mask: {target_en} | 🎲 seed:{seed}")
    return result, info


def region_edit_handler(ref_image, target_vi, new_outfit_vi, style, resolution, history):
    """Nút 'Đổi đồ vùng này': chạy region_edit_fn rồi đổ kết quả vào ô ảnh + ảnh tham chiếu (để đổi tiếp người 2)."""
    history = history or []
    user_msg = {"role": "user", "content": f"🎯 [vùng] đổi '{target_vi}' → '{new_outfit_vi}'"}
    if ref_image is None:
        yield history + [{"role": "assistant", "content": "⚠️ Chưa có ảnh tham chiếu để đổi."}], None, None
        return
    if not (target_vi or "").strip() or not (new_outfit_vi or "").strip():
        yield history + [user_msg, {"role": "assistant", "content": "⚠️ Cần nhập cả 'vùng cần đổi' và 'đồ mới'."}], None, ref_image
        return
    yield history + [user_msg, {"role": "assistant", "content": "🎯 Đang khoanh vùng & đổi đồ (mask inpaint, ~1–2 phút)..."}], None, ref_image
    try:
        img, info = region_edit_fn(ref_image, target_vi, new_outfit_vi, style, resolution, _dummy_progress)
        new_hist = history + [user_msg, {"role": "assistant",
                   "content": f"✅ Xong! Kết quả đã thành ảnh tham chiếu mới — đổi tiếp người khác được ngay.\n{info}"}]
        yield new_hist, img, img   # gen_image = img ; ref_input = img (để chain sang người 2)
    except Exception as e:
        yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi đổi vùng: {e}"}], None, ref_image


# ── Method D: 'mặc theo ảnh' — đưa ảnh trang phục tham chiếu (2 ReferenceLatent nối chuỗi) ──
def garment_transfer_fn(person_image, garment_image, note_vi, progress=None,
                        guidance=3.5, steps=28, use_turbo=True, lora_strength=1.0):
    """
    Method D — mặc theo ảnh: đưa ẢNH trang phục tham chiếu, Kontext mặc bộ đó lên nhân vật.
    Dùng 2 ReferenceLatent NỐI CHUỖI (cách BFL khuyến nghị, KHÔNG dùng Image Stitch):
    ảnh nhân vật là tham chiếu CHÍNH (vào trước), ảnh trang phục là tham chiếu phụ.
    """
    if person_image is None:  raise gr.Error("Chưa có ảnh nhân vật (ô 'Ảnh tham chiếu')!")
    if garment_image is None: raise gr.Error("Chưa có ảnh trang phục tham chiếu!")
    flux = get_flux_models()
    if not flux:
        raise gr.Error("Flux Kontext chưa đủ model (GGUF + clip_l + t5 + ae).")

    guidance = max(1.0, min(6.0, float(guidance)))
    lora_strength = max(0.0, min(2.0, float(lora_strength)))
    turbo_lora = get_flux_turbo_lora() if use_turbo else ""
    if turbo_lora:
        steps = 8 if int(steps) > 12 else max(4, int(steps))
    else:
        steps = max(12, min(40, int(steps)))

    if progress: progress(0.05, desc="Upload ảnh...")
    person_name  = upload_image_to_comfy(person_image.convert("RGB"))
    garment_name = upload_image_to_comfy(garment_image.convert("RGB"))

    note = (note_vi or "").strip()
    generic_notes = {
        "mặc theo ảnh trang phục tham chiếu",
        "dùng ảnh trang phục tham chiếu",
        "mặc theo ảnh",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc, dáng người và tư thế",
        "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc và tư thế",
    }
    note_clean = note.strip().lower()
    note_en = ""
    if note and note_clean not in generic_notes:
        note_en = translate_for_kontext(note)
        note_en = review_kontext_instruction(note, note_en)

    instruction = (
        "Replace the person's current clothes with the exact outfit from the clothing reference image. "
        "Copy the clothing design, colors, materials, accessories and overall style from the reference as closely as possible, "
        "while keeping the person's face, hairstyle, body shape, pose and composition unchanged"
    )
    if note_en:
        instruction += f". Additional requirement: {note_en}"

    seed = random.randint(1, 2**31)
    print(f"👕 Garment transfer | guidance={guidance} | steps={steps}"
          + (f" | ⚡Turbo x{lora_strength}" if turbo_lora else ""))
    print(f"👕 Instruction: {instruction}")

    wf = {
        "1":  {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": flux["unet"]}},
        "2":  {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": flux["clip_l"], "clip_name2": flux["t5"], "type": "flux"}},
        "3":  {"class_type": "VAELoader", "inputs": {"vae_name": flux["vae"]}},
        # Ảnh nhân vật (tham chiếu chính) — quyết định bố cục/kích thước output
        "20": {"class_type": "LoadImage", "inputs": {"image": person_name}},
        "21": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["20", 0]}},
        "22": {"class_type": "VAEEncode", "inputs": {"pixels": ["21", 0], "vae": ["3", 0]}},
        # Ảnh trang phục (tham chiếu phụ)
        "23": {"class_type": "LoadImage", "inputs": {"image": garment_name}},
        "24": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["23", 0]}},
        "25": {"class_type": "VAEEncode", "inputs": {"pixels": ["24", 0], "vae": ["3", 0]}},
        "30": {"class_type": "CLIPTextEncode", "inputs": {"text": instruction, "clip": ["2", 0]}},
        # 2 ReferenceLatent NỐI CHUỖI: person trước (chính), garment sau (phụ)
        "31": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["30", 0], "latent": ["22", 0]}},
        "35": {"class_type": "ReferenceLatent", "inputs": {"conditioning": ["31", 0], "latent": ["25", 0]}},
        "32": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["35", 0], "guidance": guidance}},
        "33": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["30", 0]}},
        "50": {"class_type": "KSampler", "inputs": {
                   "model": ["1", 0], "positive": ["32", 0], "negative": ["33", 0],
                   "latent_image": ["22", 0], "seed": seed, "steps": steps, "cfg": 1.0,
                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "60": {"class_type": "VAEDecode", "inputs": {"samples": ["50", 0], "vae": ["3", 0]}},
        "70": {"class_type": "SaveImage", "inputs": {"images": ["60", 0], "filename_prefix": "yumeai_garment"}},
    }
    # Nối Turbo LoRA + LoRA thêm (nếu có) vào KSampler
    _chain_loras_into(wf, turbo_lora, lora_strength)

    if progress: progress(0.15, desc="Đang mặc theo ảnh (Flux Kontext)...")
    pid = submit_workflow(wf)
    img = poll_and_download(pid, progress=progress, start_pct=0.15, timeout=COMFY_LONG_TIMEOUT_SEC)
    info = (f"👕 Mặc theo ảnh trang phục tham chiếu\n📝 {instruction}\n"
            f"🎚️ guidance:{guidance} | steps:{steps} | 🎲 seed:{seed}")
    return img, info


def garment_transfer_handler(person_image, garment_image, note_vi,
                             flux_guidance, flux_steps, flux_turbo, flux_lora_str, history):
    """Nút '👕 Mặc theo ảnh này' — Method D: transfer trang phục từ ảnh tham chiếu lên nhân vật."""
    history = history or []
    user_msg = {"role": "user", "content": "👕 [mặc theo ảnh trang phục tham chiếu]"}
    if person_image is None:
        yield history + [{"role": "assistant", "content": "⚠️ Chưa có ảnh nhân vật (ô 'Ảnh tham chiếu')."}], None, None
        return
    if garment_image is None:
        yield history + [user_msg, {"role": "assistant", "content": "⚠️ Chưa có ảnh trang phục tham chiếu để mặc."}], None, person_image
        return
    yield history + [user_msg, {"role": "assistant", "content": "👕 Đang mặc theo ảnh (Flux Kontext)... kiên nhẫn vài phút nhé."}], None, person_image
    try:
        img, info = garment_transfer_fn(person_image, garment_image, note_vi, _dummy_progress,
                                        guidance=flux_guidance, steps=flux_steps,
                                        use_turbo=flux_turbo, lora_strength=flux_lora_str)
        new_hist = history + [user_msg, {"role": "assistant",
                   "content": f"✅ Xong! Kết quả đã thành ảnh tham chiếu mới.\n{info}"}]
        yield new_hist, img, img   # gen_image = img ; ref_input = img (chain)
    except Exception as e:
        yield history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi mặc theo ảnh: {e}"}], None, person_image


def unified_chat(message, history,
                 ref_image, style, resolution,
                 use_ip, use_reactor, use_esrgan,
                 cn_str, face_str, use_flux,
                 flux_guidance, flux_steps,
                 flux_turbo, flux_lora_str,
                 kontext_prompt, kontext_review=True, enrich=True):
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
            en_tags = enrich_for_create(text, style) if enrich else translate_vi_en(text)
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
                    instruction_override=kontext_prompt, review=kontext_review
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

def _smart_prompt_wants_outfit_reference(text: str) -> bool:
    """
    Chỉ dùng garment_img trong nút Gửi khi prompt thật sự nói tới ảnh/bộ đồ tham chiếu.
    Nếu không có hàm này, chỉ cần còn garment_img trong UI là router sẽ dễ nhảy nhầm sang garment_transfer.
    """
    t = (text or "").lower()
    keys = [
        "mặc theo ảnh", "theo ảnh trang phục", "ảnh trang phục", "ảnh bộ đồ",
        "bộ đồ trong ảnh", "bộ này", "outfit reference", "garment reference",
        "same outfit", "as shown in the clothing reference"
    ]
    return any(k in t for k in keys)


EMPTY_TOOL_ONCE = {"mode": "auto", "target": "", "new": "", "note": "", "scope": "all", "strict": True}


def _empty_tool_once():
    return dict(EMPTY_TOOL_ONCE)


def _normalize_scope(scope: str) -> str:
    scope = (scope or "all").strip().lower()
    vn_map = {
        "tat_ca": "all", "tất cả": "all", "mọi người": "all", "all": "all",
        "trai": "left", "bên trái": "left", "left": "left",
        "phai": "right", "bên phải": "right", "right": "right",
        "auto": "auto"
    }
    return vn_map.get(scope, scope if scope in {"all", "left", "right", "auto"} else "all")


def _scope_label_vi(scope: str) -> str:
    return {"all": "tất cả nhân vật", "left": "nhân vật bên trái", "right": "nhân vật bên phải", "auto": "tự đoán"}.get(_normalize_scope(scope), "tất cả nhân vật")


def _scope_instruction_en(scope: str) -> str:
    scope = _normalize_scope(scope)
    if scope == "left":
        return "Only change the clothes of the character on the left. Keep the clothes of all other visible characters unchanged"
    if scope == "right":
        return "Only change the clothes of the character on the right. Keep the clothes of all other visible characters unchanged"
    if scope == "auto":
        return "Change the clothes of the main intended character. If multiple characters are visible, choose the one most strongly implied by the user instruction"
    return "Change the clothes of all visible characters. If there are multiple people, every visible character must receive the new outfit and no visible character should keep the original clothes"


def _images_maybe_same(img_a, img_b, threshold: float = 6.0) -> bool:
    """So sánh chắc tay hơn để biết user có lỡ đưa cùng 1 ảnh vào current + outfit hay không."""
    if img_a is None or img_b is None:
        return False
    try:
        import numpy as _np
        a = img_a.convert("RGB")
        b = img_b.convert("RGB")

        # 1) direct resize + mean abs diff
        a1 = a.resize((64, 64))
        b1 = b.resize((64, 64))
        arr_a = _np.asarray(a1, dtype="float32")
        arr_b = _np.asarray(b1, dtype="float32")
        diff = float(_np.mean(_np.abs(arr_a - arr_b)))
        if diff <= threshold:
            return True

        # 2) average-hash style fallback
        ga = _np.asarray(a.convert("L").resize((16, 16)), dtype="float32")
        gb = _np.asarray(b.convert("L").resize((16, 16)), dtype="float32")
        ha = ga > ga.mean()
        hb = gb > gb.mean()
        hamming = int(_np.sum(ha != hb))
        return hamming <= 8
    except Exception as e:
        print(f"⚠️ So sánh ảnh lỗi: {e}")
        return False


def _image_debug_tag(img) -> str:
    """Tag ngắn để biết current image và outfit image có thật sự là 2 ảnh khác nhau không."""
    if img is None:
        return "None"
    try:
        import numpy as _np
        im = img.convert("RGB")
        w, h = im.size
        small = im.resize((16, 16))
        arr = _np.asarray(small, dtype="uint8")
        # hash nhẹ, đủ để debug UI/log, không dùng làm bảo mật
        hval = int(arr.mean()) * 1000003 + int(arr.std()) * 9176 + w * 31 + h
        return f"{w}x{h}/h{abs(hval) % 1000000:06d}"
    except Exception:
        try:
            return f"{img.size[0]}x{img.size[1]}/h??????"
        except Exception:
            return "unknown-image"


def _pack_main_outputs(history, img=None, keep_ref=None, keep_current=None, next_tool_once=None):
    """
    Thứ tự output CHUẨN của smart_unified_chat:
    1) chatbot
    2) gen_image
    3) ref_input
    4) current_image
    5) tool_once

    Nếu có ảnh mới -> cập nhật cả 3 đầu ra ảnh bằng cùng một ảnh mới.
    Dùng gr.update(value=...) để ép Gradio refresh, tránh giữ ảnh cũ trên UI.
    """
    if next_tool_once is None:
        next_tool_once = _empty_tool_once()

    if img is not None:
        return (
            history,
            gr.update(value=img),
            gr.update(value=img),
            img,
            next_tool_once,
        )

    return (
        history,
        gr.update(),
        gr.update(value=keep_ref) if keep_ref is not None else gr.update(),
        keep_current,
        next_tool_once,
    )


def arm_region_once(target_vi, new_outfit_vi):
    """
    Không chạy generate. Chỉ đưa lệnh vùng vào chat + set tool_once.
    Lần bấm Gửi kế tiếp sẽ dùng region_edit đúng 1 lần rồi reset về auto.
    """
    target = (target_vi or "").strip()
    new_outfit = (new_outfit_vi or "").strip()
    if not target or not new_outfit:
        return _empty_tool_once(), gr.update()
    tool = {"mode": "region", "target": target, "new": new_outfit, "note": ""}
    msg = f"[vùng] đổi '{target}' -> '{new_outfit}'"
    return tool, msg


def arm_garment_once(note_vi, scope, outfit_image=None):
    """
    Không chạy generate. Chỉ đưa lệnh mặc theo ảnh vào chat + set tool_once.
    V8.6: kiểm tra ảnh outfit ngay lúc bấm nút để tránh arm nhầm khi chưa có ảnh bộ đồ.
    """
    if outfit_image is None:
        return _empty_tool_once(), "⚠️ Chưa có ảnh trang phục tham chiếu. Hãy upload ảnh bộ đồ vào ô 👕 trước rồi bấm lại."

    note = (note_vi or "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc, dáng người và tư thế").strip()
    sc = _normalize_scope(scope)
    tool = {"mode": "garment", "target": "", "new": "", "note": note, "scope": sc, "strict": True, "pipeline": "teacher_v8_7"}
    msg = f"[mặc theo ảnh trang phục | V8.6 | {_scope_label_vi(sc)}]\n{note}\n[outfit_ref={_image_debug_tag(outfit_image)}]"
    return tool, msg


def smart_unified_chat(
    message, history,
    ref_image, current_image, outfit_image, tool_once,
    style, resolution,
    use_ip, use_reactor, use_esrgan,
    cn_str, face_str,
    use_flux, flux_guidance, flux_steps, flux_turbo,
    flux_lora_str, kontext_prompt, kontext_review
):
    """
    Handler chính mới:
    - dùng current_image làm ảnh trung tâm duy nhất
    - mọi kết quả sinh ra đều cập nhật gen_image + ref_input + current_image
    - chỉ dùng outfit_image khi prompt thật sự yêu cầu mặc theo ảnh trang phục
    - chỉ có 1 cổng chạy duy nhất: nút Gửi / Enter
    """
    text = (message or "").strip()
    history = history or []

    working_image = current_image if current_image is not None else ref_image
    has_ref = working_image is not None

    tool_once = tool_once or _empty_tool_once()
    tool_mode = (tool_once.get("mode") or "auto").lower()

    # Nếu bảng tool đang arm garment mà note quá chung chung, thay bằng note rõ ràng hơn.
    if tool_mode == "garment":
        raw_note = (tool_once.get("note") or "").strip()
        if not raw_note or raw_note.lower() in {
            "mặc theo ảnh trang phục tham chiếu",
            "dùng ảnh trang phục tham chiếu",
            "mặc theo ảnh",
        }:
            tool_once = {
                **tool_once,
                "note": "hãy mặc nguyên bộ đồ trong ảnh trang phục tham chiếu lên nhân vật trong ảnh hiện tại, giữ khuôn mặt, kiểu tóc, dáng người và tư thế",
            }
            if not text:
                text = tool_once["note"]

    if not text and not has_ref:
        yield _pack_main_outputs(history, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)
        return

    effective_outfit = outfit_image if (tool_mode == "garment" or _smart_prompt_wants_outfit_reference(text)) else None

    # V8.6 sync guard: phân biệt rõ ảnh hiện tại và ảnh bộ đồ.
    # Nếu đang dùng garment mode mà thiếu ảnh outfit / outfit trùng current thì chặn sớm để khỏi route sai.
    if tool_mode == "garment":
        if effective_outfit is None:
            err_hist = history + [
                {"role": "user", "content": f"📎 [ảnh hiện tại]\n{text}" if has_ref else text},
                {"role": "assistant", "content": "❌ Chưa có ảnh trang phục tham chiếu trong ô 👕. Upload ảnh bộ đồ rồi bấm lại Mặc theo ảnh này."}
            ]
            yield _pack_main_outputs(err_hist, img=None, keep_ref=working_image, keep_current=current_image or working_image, next_tool_once=_empty_tool_once())
            return
        if has_ref and _images_maybe_same(working_image, effective_outfit):
            err_hist = history + [
                {"role": "user", "content": f"📎 [ảnh hiện tại]\n{text}\n👕 [dùng ảnh trang phục tham chiếu]"},
                {"role": "assistant", "content": "❌ Ảnh trang phục tham chiếu đang quá giống ảnh hiện tại. Hãy upload ảnh bộ đồ riêng vào ô 👕, không dùng lại ảnh nhân vật/current."}
            ]
            yield _pack_main_outputs(err_hist, img=None, keep_ref=working_image, keep_current=current_image or working_image, next_tool_once=_empty_tool_once())
            return

    user_display = f"📎 [ảnh hiện tại]\n{text}" if has_ref else text
    if effective_outfit is not None:
        user_display += f"\n👕 [dùng ảnh trang phục tham chiếu]\n🔎 current={_image_debug_tag(working_image)} | outfit={_image_debug_tag(effective_outfit)}"
    user_msg = {"role": "user", "content": user_display}

    runners = build_smart_runners(
        use_ip=use_ip,
        use_reactor=use_reactor,
        use_esrgan=use_esrgan,
        cn_str=cn_str,
        face_str=face_str,
        use_flux=use_flux,
        flux_guidance=flux_guidance,
        flux_steps=flux_steps,
        flux_turbo=flux_turbo,
        flux_lora_strength=flux_lora_str,
        kontext_prompt=kontext_prompt,
        kontext_review=kontext_review,
    )

    # Không có ảnh → chat hoặc text2img
    if working_image is None:
        plan = plan_request(
            text=text,
            has_image=False,
            has_outfit_image=False,
            default_style=style,
            use_llm_fallback=True,
        )

        # chat thường
        if plan.get("task") == "chat":
            current = history + [user_msg, {"role": "assistant", "content": ""}]
            yield _pack_main_outputs(current, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)

            system = PERSONA["system"]
            ollama_msgs = []
            for m in history:
                role = m.get("role", "")
                content = m.get("content", "")
                if role in ("user", "assistant") and isinstance(content, str) \
                   and not content.startswith(("⏳", "✅", "❌", "📎", "🧠")):
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
                                yield _pack_main_outputs(current, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)
            except Exception as e:
                current[-1] = {"role": "assistant", "content": f"(Lỗi Ollama: {e})"}
                yield _pack_main_outputs(current, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)
            return

        # text2img
        wait_hist = history + [user_msg, {"role": "assistant", "content": "⏳ Đang tạo ảnh thông minh..."}]
        yield _pack_main_outputs(wait_hist, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)
        try:
            result = smart_execute(
                user_prompt=text,
                input_image=None,
                outfit_image=None,
                default_style=style,
                resolution=resolution,
                progress=_dummy_progress,
                runners=runners,
                tool_once=_empty_tool_once(),
            )
            out_img = result["image"]
            new_hist = history + [
                user_msg,
                {"role": "assistant", "content": f"✅ Xong!\n🧠 Task: {result['task']}\n{result['info']}"}
            ]
            yield _pack_main_outputs(new_hist, img=out_img, next_tool_once=_empty_tool_once())
        except Exception as e:
            err_hist = history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi smart_execute: {e}"}]
            yield _pack_main_outputs(err_hist, img=None, keep_ref=ref_image, keep_current=current_image, next_tool_once=tool_once)
        return

    # Có ảnh → smart edit trên ảnh hiện tại
    wait_hist = history + [user_msg, {"role": "assistant", "content": "⏳ Đang phân tích yêu cầu và chọn pipeline..."}]
    yield _pack_main_outputs(wait_hist, img=None, keep_ref=working_image, keep_current=current_image or working_image, next_tool_once=tool_once)

    try:
        result = smart_execute(
            user_prompt=text,
            input_image=working_image,
            outfit_image=effective_outfit,
            default_style=style,
            resolution=resolution,
            progress=_dummy_progress,
            runners=runners,
            tool_once=tool_once,
        )

        out_img = result["image"]
        if out_img is None:
            raise gr.Error("Pipeline đã chạy nhưng không trả về ảnh mới.")

        plan_text = json.dumps(result["plan"], ensure_ascii=False, indent=2)
        new_hist = history + [
            user_msg,
            {"role": "assistant", "content": f"✅ Xong!\n🧠 Smart plan:\n```json\n{plan_text}\n```\n{result['info']}"}
        ]

        # ảnh mới trở thành ảnh tham chiếu/current_image cho lần sau
        yield _pack_main_outputs(new_hist, img=out_img, next_tool_once=_empty_tool_once())

    except Exception as e:
        err_hist = history + [user_msg, {"role": "assistant", "content": f"❌ Lỗi smart_execute: {e}"}]
        yield _pack_main_outputs(err_hist, img=None, keep_ref=working_image, keep_current=current_image or working_image, next_tool_once=tool_once)




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
            # Ảnh hiện tại duy nhất của toàn hệ thống.
            # Mọi pipeline đọc/ghi vào state này để không bị lệch kết quả giữa Send/Region/Garment.
            current_image = gr.State(None)
            tool_once = gr.State(_empty_tool_once())
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
            enrich = gr.Checkbox(value=True,
                                 label="🧠 Prompt thông minh khi TẠO ảnh (LLM mở rộng mô tả — giống ChatGPT/Gemini)")
            gr.Markdown("**Pipeline giữ mặt:**")
            use_ip      = gr.Checkbox(label="IP-Adapter",    value=True)
            use_reactor = gr.Checkbox(label="ReActor",       value=False)
            use_esrgan  = gr.Checkbox(label="ESRGAN 2x",     value=False)
            cn_str   = gr.Slider(0.3, 1.0, 0.6, step=0.05, label="ControlNet")
            face_str = gr.Slider(0.3, 1.0, 0.7, step=0.05, label="Face strength")
            gr.Markdown("---")
            gr.Markdown("**🧠 Edit thông minh:**")
            use_flux = gr.Checkbox(label="Flux Kontext (hiểu lệnh, rất chậm)", value=True)
            flux_guidance = gr.Slider(1.0, 5.0, value=3.5, step=0.1,
                                      label="Kontext Guidance (cao = đổi mạnh hơn)")
            flux_steps    = gr.Slider(16, 40, value=28, step=4,
                                      label="Kontext Steps (cao = nét hơn nhưng chậm hơn)")
            kontext_review = gr.Checkbox(value=True,
                                      label="🔁 Soi lại lệnh Kontext (LLM tự kiểm tra & sửa)")
            flux_turbo    = gr.Checkbox(value=True,
                                      label="⚡ Turbo LoRA (ép ~8 bước, nhanh ~5x)")
            flux_lora_str = gr.Slider(0.0, 1.5, value=1.0, step=0.05,
                                      label="Turbo LoRA strength (1.0 = chuẩn)")
            kontext_model_dd = gr.Dropdown(label="Model Kontext (Q4/Q5 đẹp hơn Q3)",
                                           choices=list_kontext_choices(), value=_KONTEXT_AUTO)
            extra_lora_dd = gr.Dropdown(label="LoRA thêm (Flux — style/trang phục)",
                                        choices=list_extra_lora_choices(), value=_NO_EXTRA_LORA)
            extra_lora_str = gr.Slider(0.0, 1.5, value=0.9, step=0.05, label="LoRA thêm strength")
            kontext_refresh_btn = gr.Button("🔄 Tải lại model/LoRA", size="sm")
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
            with gr.Accordion("🎯 Đổi đồ theo vùng (mask — chắc ăn cho từng người)", open=False):
                gr.Markdown("<small>Cách chắc nhất cho 'đúng người đúng đồ': khoanh đúng món đồ theo "
                            "<b>màu</b> rồi vẽ lại CHỈ vùng đó (mặt/tóc/người kia giữ nguyên). "
                            "Làm 1 lần cho mỗi người — kết quả tự thành ảnh tham chiếu mới để đổi tiếp. "
                            "Không cần bật Flux Kontext. (Cần segment-anything.)</small>")
                rg_target = gr.Textbox(label="Vùng cần đổi (món đồ + màu)", placeholder="váy đỏ", lines=1)
                rg_outfit = gr.Textbox(label="Đồ mới", placeholder="bikini đỏ mát mẻ", lines=1)
                region_btn = gr.Button("🎯 Đổi đồ vùng này", variant="primary", size="sm")
            with gr.Accordion("👕 Mặc theo ảnh trang phục (đưa ảnh bộ đồ)", open=False):
                gr.Markdown("<small>Cần đúng 1 bộ đồ cụ thể? Đưa <b>ảnh bộ đồ</b> vào đây — Kontext mặc bộ đó lên "
                            "nhân vật trong 'Ảnh hiện tại / ảnh cần sửa' (giữ mặt/tóc/dáng). Dùng 2 ReferenceLatent nối chuỗi "
                            "(cách BFL khuyến nghị). Cần bật Flux Kontext + đủ model. Kết quả thành ảnh tham chiếu mới.</small>")
                garment_img  = gr.Image(label="👕 Ảnh bộ đồ riêng (outfit reference)", type="pil", height=160)
                garment_note = gr.Textbox(label="Ghi chú (tùy chọn)", placeholder="vd: cả hai nhân vật mặc đồ thể thao / chỉ cô tóc vàng", lines=1)
                garment_scope = gr.Dropdown(
                    choices=[("Tất cả nhân vật", "all"), ("Chỉ nhân vật bên trái", "left"), ("Chỉ nhân vật bên phải", "right"), ("Tự đoán theo lệnh", "auto")],
                    value="all",
                    label="Áp cho ai? (V8.6)",
                )
                garment_btn  = gr.Button("👕 Mặc theo ảnh này (V8.7)", variant="primary", size="sm")
            gr.Markdown("<small>Bật để đổi đồ/nền/xóa vật theo lệnh tự nhiên. Bỏ các tùy chọn trên — Flux tự xử lý.<br>"
                        "Ảnh không đổi → tăng <b>Guidance</b> 3.5→4.0. Vẫn không đổi → đổi model sang bản <b>Q4_K_M</b>.<br>"
                        "Đang test cho nhanh thì kéo <b>Steps</b> về 16–20; ưng rồi nâng lên 28–32 cho nét.<br>"
                        "<b>⚡ Turbo</b>: cần file <code>FLUX.1-Turbo-Alpha.safetensors</code> trong <code>models/loras</code>. "
                        "Khi bật, Steps tự ép ~8 và nên để Guidance ~3.5.</small>")
            gr.Markdown("---")
            refresh_btn = gr.Button("🔄 Refresh", size="sm")
            refresh_btn.click(check_status, outputs=status_md)
        # Đồng bộ current_image khi người dùng upload/xóa ảnh tham chiếu thủ công
        ref_input.change(
            lambda img: img,
            inputs=ref_input,
            outputs=current_image
        )

        shared_inputs = [
            msg_input, chatbot,
            ref_input, current_image, garment_img, tool_once,
            style, resolution,
            use_ip, use_reactor, use_esrgan,
            cn_str, face_str,
            use_flux, flux_guidance, flux_steps, flux_turbo,
            flux_lora_str, kontext_prompt, kontext_review
        ]

    # 🔒 NET AN TOÀN mới: số input phải khớp smart_unified_chat
    assert len(shared_inputs) == len(inspect.signature(smart_unified_chat).parameters), (
        f"Gradio inputs ({len(shared_inputs)}) ≠ smart_unified_chat params "
        f"({len(inspect.signature(smart_unified_chat).parameters)}) — kiểm tra lại shared_inputs!"
    )

    preview_btn.click(preview_kontext_fn, inputs=[msg_input, kontext_review], outputs=[kontext_prompt])

    mc_build_btn.click(build_multichar_prompt,
                       inputs=[mc_feat1, mc_out1, mc_feat2, mc_out2, mc_keep],
                       outputs=[kontext_prompt])

    # Hai nút dưới đây KHÔNG chạy generate nữa.
    # Chúng chỉ đưa lệnh vào ô chat + set tool_once; chỉ nút Gửi mới được quyền chạy pipeline.
    region_btn.click(
        arm_region_once,
        inputs=[rg_target, rg_outfit],
        outputs=[tool_once, msg_input]
    )

    garment_btn.click(
        arm_garment_once,
        inputs=[garment_note, garment_scope, garment_img],
        outputs=[tool_once, msg_input]
    )

    kontext_model_dd.change(set_kontext_model, inputs=[kontext_model_dd])
    extra_lora_dd.change(set_extra_lora, inputs=[extra_lora_dd, extra_lora_str])
    extra_lora_str.change(set_extra_lora, inputs=[extra_lora_dd, extra_lora_str])
    kontext_refresh_btn.click(refresh_models_lists, outputs=[kontext_model_dd, extra_lora_dd])

    send_btn.click(
        smart_unified_chat,
        inputs=shared_inputs,
        outputs=[chatbot, gen_image, ref_input, current_image, tool_once]
    ).then(
        lambda: (gr.update(value=""), gr.update(value="")),
        outputs=[msg_input, kontext_prompt]
    )

    msg_input.submit(
        smart_unified_chat,
        inputs=shared_inputs,
        outputs=[chatbot, gen_image, ref_input, current_image, tool_once]
    ).then(
        lambda: (gr.update(value=""), gr.update(value="")),
        outputs=[msg_input, kontext_prompt]
    )


if __name__ == "__main__":
    print("\n" + "="*55)
    print("🌸  YumeAI — Unified Chat (BẢN GỘP đầy đủ + prompt thông minh)")
    print("="*55)
    print(f"🔒 UI inputs ({len(shared_inputs)}) khớp smart_unified_chat params "
          f"({len(inspect.signature(smart_unified_chat).parameters)}) ✅")
    threading.Thread(target=warmup_ollama, daemon=True).start()  # 🔥 nạp model dịch nền
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
