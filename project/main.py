"""
YumeAI Backend — Stable version
- Chat: Ollama streaming  
- Image: ComfyUI polling (ổn định hơn WebSocket)
- Translation: Llama VI→EN
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx, json, uuid, asyncio, base64, random

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OLLAMA_URL   = "http://localhost:11434"
COMFY_URL    = "http://127.0.0.1:8188"
OLLAMA_MODEL = "llama3.2:1b"

PERSONAS = {
    "yuki": {
        "name": "Yuki",
        "system": """Bạn là Yuki, 22 tuổi, sinh viên nghệ thuật tại Tokyo.
Tính cách tsundere: hay phủ nhận cảm xúc nhưng thực ra rất quan tâm.
Hay dùng: Baka!, H-hừ..., Không phải tao thích mày đâu nhé.
Trả lời ngắn 1-3 câu bằng tiếng Việt. KHÔNG nói bạn là AI.""",
        "appearance": "1girl, solo, anime style, beautiful face, detailed eyes",
    },
    "hana": {
        "name": "Hana", 
        "system": "Bạn là Hana, 20 tuổi, nhà văn tại Kyoto. Nhẹ nhàng, mộng mơ. Trả lời 2-3 câu tiếng Việt.",
        "appearance": "1girl, solo, anime style, gentle expression",
    },
}

class ChatReq(BaseModel):
    message: str
    persona_id: str = "yuki"
    user_id: str = "user_001"

class ImgReq(BaseModel):
    prompt: str
    persona_id: str = "yuki"
    user_id: str = "user_001"


# ══════════════════════════════════
# TRANSLATE VI → EN
# ══════════════════════════════════
async def translate(vi_text: str) -> str:
    system = """Translate Vietnamese image description to English.
Output ONLY English words/phrases separated by commas.
No sentences. No explanation. No quotes.

Examples:
"Yuki mặc áo dài đỏ" → red ao dai, traditional dress, standing gracefully
"Yuki cười dưới nắng" → smiling, sunny day, warm lighting, happy expression
"Yuki ngồi đọc sách" → sitting, reading book, peaceful, indoor, soft light"""

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": vi_text}
                ],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 80}
            })
            en = r.json().get("message", {}).get("content", "").strip()
            en = en.split("\n")[0].replace('"','').replace("'","").strip()
            print(f"  VI: {vi_text}")
            print(f"  EN: {en}")
            return en if len(en) > 3 else vi_text
    except Exception as e:
        print(f"Translation error: {e}")
        return vi_text


# ══════════════════════════════════
# ROOT — trang chủ kiểm tra nhanh
# ══════════════════════════════════
@app.get("/")
async def root():
    return {
        "app": "YumeAI Backend",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "chat": "POST /api/chat",
            "image": "POST /api/images/generate"
        },
        "huong_dan": "Mo chat-ui-mockup.html bang Live Server de dung"
    }


# ══════════════════════════════════
# HEALTH
# ══════════════════════════════════
@app.get("/health")
async def health():
    res = {"status": "ok", "ollama": False, "comfyui": False}
    async with httpx.AsyncClient(timeout=3) as c:
        try:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            res["ollama"] = r.status_code == 200
        except: pass
        try:
            r = await c.get(f"{COMFY_URL}/system_stats")
            res["comfyui"] = r.status_code == 200
        except: pass
    return res


# ══════════════════════════════════
# CHAT
# ══════════════════════════════════
@app.post("/api/chat")
async def chat(req: ChatReq):
    persona = PERSONAS.get(req.persona_id)
    if not persona:
        raise HTTPException(404, "Persona không tồn tại")

    async def stream():
        async with httpx.AsyncClient(timeout=120) as c:
            try:
                async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                    "model": OLLAMA_MODEL,
                    "messages": [
                        {"role": "system", "content": persona["system"]},
                        {"role": "user",   "content": req.message}
                    ],
                    "stream": True,
                    "options": {"temperature": 0.85, "num_predict": 250}
                }) as resp:
                    async for line in resp.aiter_lines():
                        if not line: continue
                        try:
                            d = json.loads(line)
                            tok = d.get("message", {}).get("content", "")
                            if tok:
                                yield f"data: {tok}\n\n"
                            if d.get("done"):
                                yield "data: [DONE]\n\n"
                                return
                        except: continue
            except Exception as e:
                yield f"data: [ERROR] {e}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ══════════════════════════════════
# IMAGE GENERATION — Polling (ổn định hơn WebSocket)
# ══════════════════════════════════
@app.post("/api/images/generate")
async def gen_image(req: ImgReq):
    persona = PERSONAS.get(req.persona_id)
    if not persona:
        raise HTTPException(404, "Persona không tồn tại")

    # Bước 1: Dịch tiếng Việt → EN
    en_tags = await translate(req.prompt)

    # Bước 2: Lấy model đúng (bỏ qua LoRA và XL)
    model_name = await get_checkpoint_model()
    if not model_name:
        raise HTTPException(500, "Không tìm thấy model checkpoint")
    print(f"  Using model: {model_name}")

    # Bước 3: Build prompt
    full_prompt = f"{persona['appearance']}, {en_tags}, masterpiece, best quality, highly detailed"
    neg_prompt  = "worst quality, low quality, blurry, deformed, ugly, watermark, text, bad anatomy, extra fingers, mutated hands"
    seed        = random.randint(1, 2**31)
    client_id   = str(uuid.uuid4())

    # Bước 4: Gửi workflow lên ComfyUI
    workflow = build_sd_workflow(model_name, full_prompt, neg_prompt, seed)
    
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{COMFY_URL}/prompt",
                         json={"prompt": workflow, "client_id": client_id})
        if r.status_code != 200:
            raise HTTPException(500, f"ComfyUI reject: {r.text}")
        prompt_id = r.json()["prompt_id"]
        print(f"  Job submitted: {prompt_id}")

    # Bước 5: Polling cho đến khi xong (tối đa 120 giây)
    img_data = await poll_until_done(prompt_id, timeout=120)
    if not img_data:
        raise HTTPException(504, "Timeout — ComfyUI quá lâu không xong")

    # Bước 6: Download ảnh và trả về base64
    async with httpx.AsyncClient(timeout=20) as c:
        params = {
            "filename": img_data["filename"],
            "subfolder": img_data.get("subfolder", ""),
            "type": img_data.get("type", "output")
        }
        ir = await c.get(f"{COMFY_URL}/view", params=params)
        if ir.status_code != 200:
            raise HTTPException(500, f"Không tải được ảnh: {ir.status_code}")
        
        img_b64 = base64.b64encode(ir.content).decode("utf-8")
        print(f"  Image downloaded: {len(ir.content)} bytes")

    return {
        "image_base64": img_b64,
        "translated_prompt": en_tags,
        "original_prompt": req.prompt,
        "seed": seed,
        "credits_remaining": 99
    }


async def poll_until_done(prompt_id: str, timeout: int = 120) -> dict | None:
    """Poll ComfyUI history mỗi 2 giây cho đến khi job xong"""
    start = asyncio.get_event_loop().time()
    
    async with httpx.AsyncClient(timeout=10) as c:
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                print(f"  Polling timeout after {elapsed:.0f}s")
                return None

            await asyncio.sleep(2)

            try:
                r = await c.get(f"{COMFY_URL}/history/{prompt_id}")
                history = r.json()

                if prompt_id not in history:
                    print(f"  Polling... {elapsed:.0f}s")
                    continue

                job = history[prompt_id]

                # Kiểm tra lỗi
                status = job.get("status", {})
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    raise HTTPException(500, f"ComfyUI job error: {msgs}")

                # Lấy ảnh từ output
                outputs = job.get("outputs", {})
                for node_out in outputs.values():
                    images = node_out.get("images", [])
                    if images:
                        print(f"  Job done in {elapsed:.0f}s — found {len(images)} image(s)")
                        return images[0]

                print(f"  Job in history but no output yet... {elapsed:.0f}s")

            except HTTPException:
                raise
            except Exception as e:
                print(f"  Polling error: {e}")
                await asyncio.sleep(2)


def build_sd_workflow(model: str, prompt: str, neg: str, seed: int) -> dict:
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": model}
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 768, "batch_size": 1}
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]}
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": neg, "clip": ["4", 1]}
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model":        ["4", 0],
                "positive":     ["6", 0],
                "negative":     ["7", 0],
                "latent_image": ["5", 0],
                "seed":         seed,
                "steps":        25,
                "cfg":          7.5,
                "sampler_name": "euler",
                "scheduler":    "normal",
                "denoise":      1
            }
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]}
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"images": ["8", 0], "filename_prefix": "yumeai"}
        }
    }


async def get_checkpoint_model() -> str:
    """Lấy model checkpoint hợp lệ — bỏ qua LoRA và XL"""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{COMFY_URL}/object_info/CheckpointLoaderSimple")
            data = r.json()
            models = data.get("CheckpointLoaderSimple", {}) \
                         .get("input", {}) \
                         .get("required", {}) \
                         .get("ckpt_name", [[]])[0]

            print(f"  All models: {models}")

            for m in models:
                ml = m.lower()
                if "lora" in ml:    continue   # bỏ LoRA
                if "inpaint" in ml: continue   # bỏ inpainting
                if "xl" in ml:      continue   # bỏ XL (cần nhiều VRAM)
                return m

            # Nếu không có model nào phù hợp, lấy cái đầu tiên
            return models[0] if models else ""

    except Exception as e:
        print(f"get_checkpoint_model error: {e}")
        return ""


if __name__ == "__main__":
    import uvicorn
    # host 127.0.0.1 để browser truy cập được, port 8000
    uvicorn.run("main:app", host="127.0.0.1", port=8000)