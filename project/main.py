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
import httpx, json, asyncio, base64, random, threading

from yumeai.core.config import (
    OLLAMA_URL, COMFY_URL, OLLAMA_MODEL, COMFY_TIMEOUT_SEC, CHAT_TIMEOUT_SEC,
)
from yumeai.core.personas import PERSONAS
# Lịch sử chat dùng CHUNG với app_gradio.py — trước đây endpoint này không có
# lịch sử gì cả nên nhân vật mất trí nhớ sau mỗi câu.
from yumeai.core.chat_context import build_persona_messages, chat_options
# Prompt text2img dùng CHUNG với app_gradio.py — hai cửa vào phải cho ra cùng một ảnh
from yumeai.core.image_prompt import build_positive_prompt, NEGATIVE_PROMPT
from yumeai.core.runtime import run_comfy_job, ResourceBusyError
from yumeai.services.ollama_service import (
    translate_vi_en_ex, enrich_for_create_ex, warmup_ollama, check_chat_model,
)
from yumeai.services.comfy_service import (
    ComfyUIError,
    get_checkpoint_model,
    build_quality_workflow,
    QUALITY_BALANCED,
    UPSCALE_NONE,
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatReq(BaseModel):
    message: str
    persona_id: str = "yuki"
    user_id: str = "user_001"
    user_name: str = ""   # tên người dùng, để nhân vật gọi đúng
    # [{"role": "user"|"assistant", "content": "..."}] — client gửi lên các lượt trước.
    # Server chưa có DB nên trí nhớ vẫn nằm ở client; đây là bước đệm trước khi
    # có bảng conversation/message thật.
    history: list = []


class ImgReq(BaseModel):
    prompt: str
    persona_id: str = "yuki"
    user_id: str = "user_001"
    style: str = "Anime"          # Anime | Realistic | Semi
    quality: str = QUALITY_BALANCED
    upscale: str = UPSCALE_NONE
    # Mở rộng mô tả ngắn thành prompt chi tiết bằng LLM (giống checkbox
    # "🧠 Prompt thông minh" bên app_gradio.py).
    enrich: bool = True


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
            "personas": "/api/personas",
            "chat": "POST /api/chat",
            "image": "POST /api/images/generate",
        },
        "ghi_chu": "Backend này dùng chung logic với app_gradio.py qua package yumeai/. "
                   "Muốn dùng giao diện chat thì chạy app_gradio.py, không phải file này.",
    }


@app.on_event("startup")
async def _startup():
    """Nạp sẵn model vào RAM. app_gradio.py đã làm việc này từ lâu còn main.py thì
    không — nên request đầu tiên qua API rất hay timeout."""
    warn = check_chat_model()
    if warn:
        print(warn)
    threading.Thread(target=warmup_ollama, daemon=True).start()


# ══════════════════════════════════
# PERSONAS — cho web UI dựng danh sách nhân vật
# ══════════════════════════════════
@app.get("/api/personas")
async def list_personas():
    return {
        "personas": [
            {
                "id": pid,
                "name": p.get("name", pid),
                "age": p.get("age"),
                "role": p.get("role", ""),
                "greeting": p.get("greeting", ""),
                "likes": p.get("likes", []),
                "appearance": p.get("appearance", ""),
            }
            for pid, p in PERSONAS.items()
        ]
    }


# ══════════════════════════════════
# HEALTH
# ══════════════════════════════════
@app.get("/health")
async def health():
    res = {
        "status": "ok",
        "ollama": False,     # BẮT BUỘC: chat, dịch VI→EN, vision
        "comfyui": False,    # BẮT BUỘC: mọi việc vẽ/sửa ảnh đều chạy ở đây
    }
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

    messages = build_persona_messages(persona, req.history, req.message,
                                      user_name=req.user_name)

    async def stream():
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_SEC) as c:
            try:
                async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": True,
                    "options": chat_options(),
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

    # Bước 1: Dịch tiếng Việt → EN (dùng chung ollama_service, không tự viết lại).
    # translate_ok=False nghĩa là Ollama không phản hồi và en_tags VẪN LÀ tiếng Việt —
    # Stable Diffusion không hiểu tiếng Việt nên ảnh sẽ rác. Phải báo cho client biết,
    # bản cũ nuốt lỗi im lặng.
    if req.enrich:
        en_tags, translate_ok = await asyncio.to_thread(enrich_for_create_ex, req.prompt, req.style)
    else:
        en_tags, translate_ok = await asyncio.to_thread(translate_vi_en_ex, req.prompt)

    # Bước 2: Lấy model checkpoint hợp lệ (loại SDXL/LoRA/inpaint — máy 4GB VRAM).
    # get_checkpoint_model nay ném ComfyUIError kèm hướng dẫn cụ thể khi ComfyUI chưa
    # bật, thay vì trả "" rồi báo "Không tìm thấy model checkpoint" gây hiểu nhầm.
    try:
        model_name = await asyncio.to_thread(get_checkpoint_model, req.style)
    except ComfyUIError as e:
        raise HTTPException(502, str(e))
    if not model_name:
        raise HTTPException(502, "ComfyUI không trả về checkpoint nào dùng được.")

    # Bước 3: Build prompt + workflow (dùng chung comfy_service, có quality/upscale preset)
    full_prompt = build_positive_prompt(en_tags, req.style, persona["appearance"])
    seed = random.randint(1, 2**31)

    workflow = build_quality_workflow(
        model_name, full_prompt, NEGATIVE_PROMPT, seed,
        quality=req.quality, upscale=req.upscale,
    )

    # Bước 4-5: Gửi job + poll cho đến khi xong — có Resource Guard dùng chung
    # với app_gradio.py (chặn 2 job ComfyUI chạy chồng, tránh tràn 4GB VRAM).
    # LƯU Ý: guard này chỉ chia sẻ được nếu app_gradio.py và main.py chạy CHUNG
    # 1 tiến trình Python. Nếu chạy 2 tiến trình `python` riêng biệt cùng lúc,
    # hãy tự đảm bảo không bấm tạo ảnh ở cả hai nơi cùng lúc.
    try:
        # run_comfy_job giữ slot trong MỘT khối: submit và poll không còn tách đôi
        # ở hai hàm khác nhau nữa (xem comfy_job() trong yumeai/core/runtime.py).
        img = await asyncio.to_thread(run_comfy_job, workflow, None, 0.0, COMFY_TIMEOUT_SEC)
    except ResourceBusyError as e:
        raise HTTPException(429, str(e))
    except ComfyUIError as e:
        # Lỗi từ ComfyUI (từ chối job / job lỗi / timeout) — trả 502 thay vì 500 khó hiểu.
        raise HTTPException(502, str(e))

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
        "style": req.style,
        "seed": seed,
        "translation_ok": translate_ok,
        "warning": None if translate_ok else (
            "Không dịch được sang tiếng Anh (Ollama không phản hồi). Prompt tiếng Việt "
            "đã đi thẳng vào Stable Diffusion nên ảnh có thể sai hoặc rác."
        ),
    }


if __name__ == "__main__":
    import uvicorn
    # host 127.0.0.1 để browser truy cập được, port 8000
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
