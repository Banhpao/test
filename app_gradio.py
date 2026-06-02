"""
YumeAI — Gradio GUI (All-in-one, dễ debug)
Chat (Ollama) + Tạo ảnh (ComfyUI) trong 1 file Python

Cài: pip install gradio httpx pillow
Chạy: python app_gradio.py
Mở:  http://localhost:7860
"""

import gradio as gr
import httpx
import json
import uuid
import time
import io
import random
from PIL import Image

# ══════════════════════════════════
# CẤU HÌNH
# ══════════════════════════════════
OLLAMA_URL   = "http://localhost:11434"
COMFY_URL    = "http://127.0.0.1:8188"
OLLAMA_MODEL = "llama3.2:1b"

PERSONA = {
    "name": "Yuki",
    "system": """Bạn là Yuki, 22 tuổi, sinh viên nghệ thuật tại Tokyo.
Tính cách tsundere: hay phủ nhận cảm xúc nhưng thực ra rất quan tâm.
Hay dùng: Baka!, H-hừ..., Không phải tao thích mày đâu nhé.
Trả lời ngắn 1-3 câu bằng tiếng Việt. KHÔNG nói bạn là AI.""",
    "appearance": "1girl, solo, anime style, beautiful detailed face, expressive eyes",
}


# ══════════════════════════════════
# DỊCH VI → EN
# ══════════════════════════════════
def translate_vi_en(vi_text: str) -> str:
    system = """Translate Vietnamese image description to English tags.
Output ONLY comma-separated English words. No sentences. No quotes.

Examples:
"Yuki mặc áo dài đỏ" → red ao dai, traditional dress, standing
"Yuki cười dưới nắng" → smiling, sunny, warm light, happy
"Yuki ngồi đọc sách" → sitting, reading book, peaceful, indoor"""

    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": vi_text}
                ],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 80}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = en.split("\n")[0].replace('"', '').replace("'", "").strip()
            print(f"🔤 VI: {vi_text}")
            print(f"🔤 EN: {en}")
            return en if len(en) > 3 else vi_text
    except Exception as e:
        print(f"❌ Translation error: {e}")
        return vi_text


# ══════════════════════════════════
# CHAT — Ollama streaming
# ══════════════════════════════════
def chat_fn(message, history):
    """Generator stream phản hồi từ Ollama"""
    if not message.strip():
        yield ""
        return

    # Build messages từ history
    messages = [{"role": "system", "content": PERSONA["system"]}]
    for user_msg, bot_msg in history:
        messages.append({"role": "user", "content": user_msg})
        if bot_msg:
            messages.append({"role": "assistant", "content": bot_msg})
    messages.append({"role": "user", "content": message})

    partial = ""
    try:
        with httpx.Client(timeout=120) as c:
            with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "stream": True,
                "options": {"temperature": 0.85, "num_predict": 250}
            }) as resp:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        tok = data.get("message", {}).get("content", "")
                        partial += tok
                        yield partial
                        if data.get("done"):
                            return
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        yield f"⚠️ Lỗi: {e}"


# ══════════════════════════════════
# TẠO ẢNH — ComfyUI
# ══════════════════════════════════
def get_checkpoint_model() -> str:
    """Lấy model checkpoint hợp lệ (bỏ LoRA, XL)"""
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/object_info/CheckpointLoaderSimple")
            models = r.json().get("CheckpointLoaderSimple", {}) \
                            .get("input", {}).get("required", {}) \
                            .get("ckpt_name", [[]])[0]
            print(f"📦 Models: {models}")
            for m in models:
                ml = m.lower()
                if "lora" in ml or "inpaint" in ml or "xl" in ml:
                    continue
                return m
            return models[0] if models else ""
    except Exception as e:
        print(f"❌ get_model error: {e}")
        return ""


def generate_image_fn(prompt, progress=gr.Progress()):
    """Tạo ảnh từ prompt tiếng Việt, trả về PIL Image"""
    if not prompt.strip():
        raise gr.Error("Hãy nhập mô tả ảnh!")

    progress(0.1, desc="Đang dịch sang tiếng Anh...")
    en_tags = translate_vi_en(prompt)

    progress(0.2, desc="Đang chuẩn bị model...")
    model = get_checkpoint_model()
    if not model:
        raise gr.Error("Không tìm thấy model trong ComfyUI!")
    print(f"🎨 Using model: {model}")

    # Build workflow
    full_prompt = f"{PERSONA['appearance']}, {en_tags}, masterpiece, best quality, highly detailed"
    neg_prompt  = "worst quality, low quality, blurry, deformed, ugly, watermark, text, bad anatomy, extra fingers"
    seed        = random.randint(1, 2**31)
    client_id   = str(uuid.uuid4())

    workflow = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": full_prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": neg_prompt, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0], "seed": seed,
            "steps": 25, "cfg": 7.5, "sampler_name": "euler", "scheduler": "normal", "denoise": 1
        }},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "yumeai"}}
    }

    # Submit job
    progress(0.3, desc="Đang gửi lệnh tới ComfyUI...")
    with httpx.Client(timeout=15) as c:
        r = c.post(f"{COMFY_URL}/prompt", json={"prompt": workflow, "client_id": client_id})
        if r.status_code != 200:
            raise gr.Error(f"ComfyUI từ chối: {r.text}")
        prompt_id = r.json()["prompt_id"]
        print(f"📤 Job submitted: {prompt_id}")

    # Poll cho đến khi xong
    start = time.time()
    img_info = None
    with httpx.Client(timeout=10) as c:
        while time.time() - start < 120:
            time.sleep(2)
            elapsed = time.time() - start
            progress(min(0.3 + elapsed / 60, 0.9), desc=f"ComfyUI đang vẽ... {elapsed:.0f}s")

            try:
                hr = c.get(f"{COMFY_URL}/history/{prompt_id}")
                history = hr.json()
                if prompt_id not in history:
                    print(f"⏳ Polling... {elapsed:.0f}s")
                    continue

                job = history[prompt_id]
                status = job.get("status", {})
                if status.get("status_str") == "error":
                    raise gr.Error(f"ComfyUI lỗi: {status.get('messages', [])}")

                outputs = job.get("outputs", {})
                for node_out in outputs.values():
                    images = node_out.get("images", [])
                    if images:
                        img_info = images[0]
                        print(f"✅ Done in {elapsed:.0f}s")
                        break
                if img_info:
                    break
            except gr.Error:
                raise
            except Exception as e:
                print(f"⚠️ Poll error: {e}")

    if not img_info:
        raise gr.Error("Timeout — ComfyUI quá lâu không xong")

    # Download ảnh → PIL Image (Gradio hiển thị trực tiếp)
    progress(0.95, desc="Đang tải ảnh...")
    with httpx.Client(timeout=20) as c:
        params = {
            "filename": img_info["filename"],
            "subfolder": img_info.get("subfolder", ""),
            "type": img_info.get("type", "output")
        }
        ir = c.get(f"{COMFY_URL}/view", params=params)
        img = Image.open(io.BytesIO(ir.content))
        print(f"🖼️ Image loaded: {img.size}, {len(ir.content)} bytes")

    progress(1.0, desc="Xong!")
    return img, f"✨ Prompt: {prompt}\n🔤 Dịch: {en_tags}\n🎲 Seed: {seed}"


# ══════════════════════════════════
# KIỂM TRA KẾT NỐI
# ══════════════════════════════════
def check_status():
    ollama_ok = comfy_ok = False
    try:
        with httpx.Client(timeout=3) as c:
            ollama_ok = c.get(f"{OLLAMA_URL}/api/tags").status_code == 200
    except: pass
    try:
        with httpx.Client(timeout=3) as c:
            comfy_ok = c.get(f"{COMFY_URL}/system_stats").status_code == 200
    except: pass

    o = "🟢 Ollama" if ollama_ok else "🔴 Ollama"
    cf = "🟢 ComfyUI" if comfy_ok else "🔴 ComfyUI"
    return f"{o}  |  {cf}"


# ══════════════════════════════════
# GIAO DIỆN GRADIO
# ══════════════════════════════════
with gr.Blocks(title="YumeAI", theme=gr.themes.Soft(primary_hue="purple")) as demo:
    gr.Markdown("# 🌸 YumeAI — Local AI Companion")
    status = gr.Markdown(check_status())

    with gr.Tabs():
        # ── TAB CHAT ──
        with gr.Tab("💬 Chat với Yuki"):
            gr.ChatInterface(
                fn=chat_fn,
                chatbot=gr.Chatbot(height=450, label="Yuki 🌸"),
                textbox=gr.Textbox(placeholder="Nhắn với Yuki...", container=False),
                examples=[
                    "Yuki hôm nay thế nào?",
                    "Kể về Tokyo đi Yuki",
                    "Yuki thích vẽ gì nhất?",
                ],
            )

        # ── TAB TẠO ẢNH ──
        with gr.Tab("🎨 Tạo ảnh"):
            with gr.Row():
                with gr.Column(scale=1):
                    img_prompt = gr.Textbox(
                        label="Mô tả ảnh (tiếng Việt)",
                        placeholder="vd: Yuki mặc áo dài đỏ đứng bên hoa anh đào",
                        lines=3
                    )
                    gen_btn = gr.Button("🎨 Tạo ảnh", variant="primary", size="lg")
                    gr.Examples(
                        examples=[
                            "Yuki mặc kimono xanh ngắm hoa anh đào",
                            "Yuki cười tươi dưới ánh nắng buổi sáng",
                            "Yuki ngồi vẽ tranh bên cửa sổ",
                            "Yuki mặc đồng phục học sinh, tóc buộc đuôi ngựa",
                        ],
                        inputs=img_prompt
                    )
                    img_info = gr.Textbox(label="Thông tin", lines=3, interactive=False)

                with gr.Column(scale=1):
                    img_output = gr.Image(label="Ảnh tạo ra", height=500)

            gen_btn.click(
                fn=generate_image_fn,
                inputs=img_prompt,
                outputs=[img_output, img_info]
            )

    # Nút refresh status
    refresh_btn = gr.Button("🔄 Kiểm tra kết nối", size="sm")
    refresh_btn.click(fn=check_status, outputs=status)


if __name__ == "__main__":
    print("\n" + "="*50)
    print("🌸 YumeAI đang khởi động...")
    print("="*50)
    print(check_status())
    print("="*50 + "\n")
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)