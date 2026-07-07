"""
YumeAI Backend — FastAPI (REST API thuần, không có Gradio UI)
Dùng lại 100% logic từ yumeai/ — KHÔNG tự định nghĩa lại persona/dịch/workflow
để tránh lệch pha với app_gradio.py (bài học từ lần review trước).

- Chat: Ollama streaming
- Image: ComfyUI polling (ổn định hơn WebSocket)
- Translation + workflow builder: dùng chung yumeai.services
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx, json, uuid, asyncio, base64, random

from yumeai.core.config import OLLAMA_URL, COMFY_URL, OLLAMA_MODEL
from yumeai.core.personas import PERSONAS
from yumeai.core.runtime import (
    guarded_submit_workflow,
    guarded_poll_and_download,
    ResourceBusyError,
)
from yumeai.services.ollama_service import translate_vi_en
from yumeai.services.comfy_service import (
    get_checkpoint_model,
    build_quality_workflow,
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatReq(BaseModel):
    message: str
    persona_id: str = "yuki"
    user_id: str = "user_001"


class ImgReq(BaseModel):
    prompt: str
    persona_id: str = "yuki"
    user_id: str = "user_001"
    quality: str = "Cân bằng (28 steps)"
    upscale: str = "Không"


# ══════════════════════════════════
# ROOT — trang chủ kiểm tra nhanh
# ══════════════════════════════════
@app.get("/")
async def root():
    return {
        "app": "YumeAI Backend (REST API)",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "chat": "POST /api/chat",
            "image": "POST /api/images/generate",
        },
        "ghi_chu": "Backend này dùng chung logic với app_gradio.py qua package yumeai/. "
                   "Muốn dùng giao diện chat thì chạy app_gradio.py, không phải file này.",
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
        except Exception:
            pass
        try:
            r = await c.get(f"{COMFY_URL}/system_stats")
            res["comfyui"] = r.status_code == 200
        except Exception:
            pass
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
                        {"role": "user", "content": req.message},
                    ],
                    "stream": True,
                    "options": {"temperature": 0.85, "num_predict": 250},
                }) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            tok = d.get("message", {}).get("content", "")
                            if tok:
                                yield f"data: {tok}\n\n"
                            if d.get("done"):
                                yield "data: [DONE]\n\n"
                                return
                        except Exception:
                            continue
            except Exception as e:
                yield f"data: [ERROR] {e}\n\n"

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ══════════════════════════════════
# IMAGE GENERATION — Polling (ổn định hơn WebSocket)
# Dùng chung build_quality_workflow + comfy_service với app_gradio.py
# ══════════════════════════════════
@app.post("/api/images/generate")
async def gen_image(req: ImgReq):
    persona = PERSONAS.get(req.persona_id)
    if not persona:
        raise HTTPException(404, "Persona không tồn tại")

    # Bước 1: Dịch tiếng Việt → EN (dùng chung ollama_service, không tự viết lại)
    en_tags = await asyncio.to_thread(translate_vi_en, req.prompt)

    # Bước 2: Lấy model checkpoint hợp lệ (loại SDXL/LoRA/inpaint — máy 4GB VRAM)
    model_name = await asyncio.to_thread(get_checkpoint_model, "Anime")
    if not model_name:
        raise HTTPException(500, "Không tìm thấy model checkpoint")

    # Bước 3: Build prompt + workflow (dùng chung comfy_service, có quality/upscale preset)
    full_prompt = f"{persona['appearance']}, {en_tags}, masterpiece, best quality, highly detailed"
    neg_prompt = "worst quality, low quality, blurry, deformed, ugly, watermark, text, bad anatomy, extra fingers, mutated hands"
    seed = random.randint(1, 2**31)

    workflow = build_quality_workflow(
        model_name, full_prompt, neg_prompt, seed,
        quality=req.quality, upscale=req.upscale,
    )

    # Bước 4-5: Gửi job + poll cho đến khi xong — có Resource Guard dùng chung
    # với app_gradio.py (chặn 2 job ComfyUI chạy chồng, tránh tràn 4GB VRAM).
    # LƯU Ý: guard này chỉ chia sẻ được nếu app_gradio.py và main.py chạy CHUNG
    # 1 tiến trình Python. Nếu chạy 2 tiến trình `python` riêng biệt cùng lúc,
    # hãy tự đảm bảo không bấm tạo ảnh ở cả hai nơi cùng lúc.
    try:
        prompt_id = await asyncio.to_thread(guarded_submit_workflow, workflow)
        img = await asyncio.to_thread(guarded_poll_and_download, prompt_id, None, 0.0, 120)
    except ResourceBusyError as e:
        raise HTTPException(429, str(e))

    # Bước 6: Encode base64 để trả về JSON
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {
        "image_base64": img_b64,
        "translated_prompt": en_tags,
        "original_prompt": req.prompt,
        "model_used": model_name,
        "seed": seed,
    }


if __name__ == "__main__":
    import uvicorn
    # host 127.0.0.1 để browser truy cập được, port 8000
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
