"""
YumeAI Backend — FastAPI (REST API thuần, không có Gradio UI)
Dùng lại 100% logic từ yumeai/ — KHÔNG tự định nghĩa lại persona/dịch/workflow
để tránh lệch pha với app_gradio.py (bài học từ lần review trước).

- Chat: Ollama streaming
- Image: ComfyUI polling (ổn định hơn WebSocket)
- Translation + workflow builder: dùng chung yumeai.services
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
import httpx, json, asyncio, base64, random, threading
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from yumeai.core.config import (
    OLLAMA_URL, COMFY_URL, OLLAMA_MODEL, COMFY_TIMEOUT_SEC, CHAT_TIMEOUT_SEC,
    DATABASE_PATH, GENERATED_IMAGES_DIR, MEMORY_EXTRACTION_EVERY_MESSAGES,
    MEMORY_FACTS_ENABLED,
)
from yumeai.core.personas import PERSONAS
# Lịch sử chat dùng CHUNG với app_gradio.py — trước đây endpoint này không có
# lịch sử gì cả nên nhân vật mất trí nhớ sau mỗi câu.
from yumeai.core.chat_context import (
    build_persona_messages,
    chat_options,
    summarize_old_history,
)
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
from yumeai.services.memory_service import (
    format_memory_context,
    refresh_conversation_memory,
)
from yumeai.storage import YumeDatabase

Path(GENERATED_IMAGES_DIR).mkdir(parents=True, exist_ok=True)
database = YumeDatabase(DATABASE_PATH)
WEB_UI_PATH = Path(__file__).with_name("chat-ui-mockup.html")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Khởi tạo DB trước khi nhận request và warmup model ở background."""
    await asyncio.to_thread(database.initialize, PERSONAS)
    warn = check_chat_model()
    if warn:
        print(warn)
    threading.Thread(target=warmup_ollama, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount(
    "/generated",
    StaticFiles(directory=GENERATED_IMAGES_DIR),
    name="generated-images",
)


class ChatReq(BaseModel):
    message: str
    persona_id: str = "yuki"
    user_id: str = "user_001"
    user_name: str = ""   # tên người dùng, để nhân vật gọi đúng
    conversation_id: str | None = None
    # Giữ để tương thích client cũ. Khi có conversation_id, server luôn đọc lịch
    # sử thật từ SQLite và bỏ qua trường này.
    history: list = Field(default_factory=list)


class ImgReq(BaseModel):
    prompt: str
    persona_id: str = "yuki"
    user_id: str = "user_001"
    conversation_id: str | None = None
    style: str = "Anime"          # Anime | Realistic | Semi
    quality: str = QUALITY_BALANCED
    upscale: str = UPSCALE_NONE
    # Mở rộng mô tả ngắn thành prompt chi tiết bằng LLM (giống checkbox
    # "🧠 Prompt thông minh" bên app_gradio.py).
    enrich: bool = True
    # Client mới dùng image_url để tránh JSON phình ~33%; giữ True làm mặc định
    # để client cũ chưa đổi vẫn nhận được image_base64.
    include_base64: bool = True


class ConversationCreateReq(BaseModel):
    user_id: str = "user_001"
    persona_id: str = "yuki"
    user_name: str = ""
    title: str = ""


class MemoryFactReq(BaseModel):
    user_id: str = "user_001"
    persona_id: str = "yuki"
    value: str
    confidence: float = 1.0


# ══════════════════════════════════
# ROOT — trang chủ kiểm tra nhanh
# ══════════════════════════════════
@app.get("/")
async def root():
    return {
        "app": "YumeAI Backend (REST API)",
        "status": "running",
        "endpoints": {
            "web_app": "/app",
            "health": "/health",
            "personas": "/api/personas",
            "conversations": "/api/conversations",
            "chat": "POST /api/chat",
            "image": "POST /api/images/generate",
            "gallery": "/api/gallery",
            "memory": "/api/memory",
        },
        "database": DATABASE_PATH,
        "ghi_chu": "Backend dùng chung logic với app_gradio.py qua package yumeai/.",
    }


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


@app.get("/app", include_in_schema=False)
async def web_app():
    """Giao diện web thật, dùng API conversation/gallery thay cho mock data."""
    return FileResponse(WEB_UI_PATH)


# ══════════════════════════════════
# CONVERSATIONS — lịch sử bền vững trong SQLite
# ══════════════════════════════════
async def _owned_conversation(
    conversation_id: str,
    user_id: str,
    persona_id: str | None = None,
) -> dict:
    conversation = await asyncio.to_thread(database.get_conversation, conversation_id)
    # Trả 404 thay vì làm lộ id hội thoại của user khác.
    if not conversation or conversation["user_id"] != user_id:
        raise HTTPException(404, "Cuộc hội thoại không tồn tại")
    if persona_id and conversation["character_id"] != persona_id:
        raise HTTPException(409, "Nhân vật không khớp với cuộc hội thoại")
    return conversation


@app.post("/api/conversations")
async def create_conversation(req: ConversationCreateReq):
    persona = PERSONAS.get(req.persona_id)
    if not persona:
        raise HTTPException(404, "Persona không tồn tại")
    try:
        conversation = await asyncio.to_thread(
            database.create_conversation,
            req.user_id,
            req.persona_id,
            title=req.title,
            display_name=req.user_name,
        )
        greeting = persona.get("greeting", "").strip()
        if greeting:
            await asyncio.to_thread(
                database.append_message,
                conversation["id"],
                "assistant",
                greeting,
            )
        return await asyncio.to_thread(database.get_conversation, conversation["id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/conversations")
async def list_conversations(user_id: str = "user_001"):
    rows = await asyncio.to_thread(database.list_conversations, user_id)
    return {"conversations": rows}


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, user_id: str = "user_001"):
    conversation = await _owned_conversation(conversation_id, user_id)
    messages = await asyncio.to_thread(database.list_messages, conversation_id)
    return {"conversation": conversation, "messages": messages}


@app.get("/api/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: str, user_id: str = "user_001"):
    await _owned_conversation(conversation_id, user_id)
    messages = await asyncio.to_thread(database.list_messages, conversation_id)
    return {"messages": messages}


@app.get("/api/gallery")
async def gallery(
    user_id: str = "user_001",
    conversation_id: str | None = None,
    limit: int = 100,
):
    if conversation_id:
        await _owned_conversation(conversation_id, user_id)
    rows = await asyncio.to_thread(
        database.list_images,
        user_id=user_id,
        conversation_id=conversation_id,
        limit=limit,
    )
    for row in rows:
        row["image_url"] = f"/generated/{Path(row['file_path']).name}"
    return {"images": rows}


# ══════════════════════════════════
# MEMORY FACTS — trí nhớ có thể xem và sửa từ UI
# ══════════════════════════════════
@app.get("/api/memory")
async def list_memory(
    user_id: str = "user_001",
    persona_id: str = "yuki",
):
    if persona_id not in PERSONAS:
        raise HTTPException(404, "Persona không tồn tại")
    facts = await asyncio.to_thread(
        database.list_memory_facts, user_id, persona_id
    )
    return {
        "facts": facts,
        "auto_extraction": MEMORY_FACTS_ENABLED,
        "extraction_every_messages": MEMORY_EXTRACTION_EVERY_MESSAGES,
    }


@app.put("/api/memory/{fact_key}")
async def put_memory(fact_key: str, req: MemoryFactReq):
    if req.persona_id not in PERSONAS:
        raise HTTPException(404, "Persona không tồn tại")
    value = req.value.strip()
    if not fact_key.strip() or not value:
        raise HTTPException(400, "Khoá và giá trị trí nhớ không được để trống")
    await asyncio.to_thread(database.ensure_user, req.user_id)
    try:
        fact = await asyncio.to_thread(
            database.upsert_memory_fact,
            req.user_id,
            req.persona_id,
            fact_key,
            value,
            confidence=max(0.0, min(req.confidence, 1.0)),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return fact


@app.delete("/api/memory/{fact_key}")
async def delete_memory(
    fact_key: str,
    user_id: str = "user_001",
    persona_id: str = "yuki",
):
    deleted = await asyncio.to_thread(
        database.delete_memory_fact, user_id, persona_id, fact_key
    )
    if not deleted:
        raise HTTPException(404, "Không tìm thấy trí nhớ")
    return {"deleted": True, "fact_key": fact_key}


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
    user_message = req.message.strip()
    if not user_message:
        raise HTTPException(400, "Tin nhắn không được để trống")

    history = req.history
    conversation = None
    memory_context = None
    if req.conversation_id:
        conversation = await _owned_conversation(
            req.conversation_id, req.user_id, req.persona_id
        )
        stored_messages = await asyncio.to_thread(
            database.list_messages, req.conversation_id
        )
        # Tin nhắn ảnh có JSON để UI dựng lại gallery; không đưa JSON thô đó vào
        # prompt vì model sẽ bắt chước cấu trúc máy móc.
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in stored_messages
            if item["content_type"] == "text"
        ]
        await asyncio.to_thread(
            database.append_message,
            req.conversation_id,
            "user",
            user_message,
        )

        facts = await asyncio.to_thread(
            database.list_memory_facts,
            conversation["user_id"],
            conversation["character_id"],
        )
        fact_context = format_memory_context(facts)
        session_summary = await asyncio.to_thread(
            summarize_old_history,
            history,
            persona.get("name", ""),
        )
        stored_summary = (conversation.get("memory_summary") or "").strip()
        # Ghi lại tóm tắt để nó SỐNG QUA restart. Trước đây cột memory_summary được
        # đọc nhưng không có chỗ nào ghi, nên nó vĩnh viễn rỗng và mỗi lần mở lại app
        # là tính lại từ đầu.
        if session_summary and session_summary != stored_summary:
            await asyncio.to_thread(
                database.update_memory_summary,
                req.conversation_id,
                session_summary,
            )
        # Dùng bản vừa tính, KHÔNG ghép với bản đã lưu — hai bản gần như trùng nhau nên
        # ghép lại là nhét trí nhớ hai lần vào system prompt. Bản đã lưu chỉ là lưới đỡ
        # cho lúc Ollama chết (session_summary rỗng) hoặc vừa mở lại app.
        long_memory = session_summary or stored_summary
        memory_context = "\n".join(
            part for part in (long_memory, fact_context) if part
        )

    # Tóm tắt lịch sử có thể gọi Ollama nên không được chặn event loop FastAPI.
    messages = await asyncio.to_thread(
        build_persona_messages,
        persona,
        history,
        user_message,
        # Chuỗi rỗng là có chủ đích: persistent route đã tự dựng memory ở trên,
        # không để build_persona_messages gọi summarizer lần thứ hai.
        memory_summary=memory_context if conversation else None,
        user_name=req.user_name or (conversation or {}).get("user_name", ""),
    )

    completion = {"done": False}

    async def stream():
        reply_parts = []
        async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_SEC) as c:
            try:
                async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": True,
                    "options": chat_options(),
                }) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        tok = d.get("message", {}).get("content", "")
                        if tok:
                            reply_parts.append(tok)
                            payload = json.dumps({"token": tok}, ensure_ascii=False)
                            yield f"data: {payload}\n\n"
                        if d.get("done"):
                            full_reply = "".join(reply_parts).strip()
                            if req.conversation_id and full_reply:
                                await asyncio.to_thread(
                                    database.append_message,
                                    req.conversation_id,
                                    "assistant",
                                    full_reply,
                                )
                            completion["done"] = True
                            yield 'data: {"done":true}\n\n'
                            return
            except Exception as e:
                payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

    async def refresh_memory_after_stream():
        if req.conversation_id and completion["done"]:
            await asyncio.to_thread(
                refresh_conversation_memory,
                database,
                req.conversation_id,
            )

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(refresh_memory_after_stream),
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
    image_prompt = req.prompt.strip()
    if not image_prompt:
        raise HTTPException(400, "Mô tả ảnh không được để trống")
    if req.conversation_id:
        await _owned_conversation(req.conversation_id, req.user_id, req.persona_id)

    # Bước 1: Dịch tiếng Việt → EN (dùng chung ollama_service, không tự viết lại).
    # translate_ok=False nghĩa là Ollama không phản hồi và en_tags VẪN LÀ tiếng Việt —
    # Stable Diffusion không hiểu tiếng Việt nên ảnh sẽ rác. Phải báo cho client biết,
    # bản cũ nuốt lỗi im lặng.
    if req.enrich:
        en_tags, translate_ok = await asyncio.to_thread(enrich_for_create_ex, image_prompt, req.style)
    else:
        en_tags, translate_ok = await asyncio.to_thread(translate_vi_en_ex, image_prompt)

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
    # Nếu có `filelock`, guard còn chia sẻ slot giữa app_gradio.py và main.py khi
    # chúng chạy ở hai tiến trình khác nhau; thiếu dependency thì runtime cảnh báo.
    try:
        # run_comfy_job giữ slot trong MỘT khối: submit và poll không còn tách đôi
        # ở hai hàm khác nhau nữa (xem comfy_job() trong yumeai/core/runtime.py).
        img = await asyncio.to_thread(run_comfy_job, workflow, None, 0.0, COMFY_TIMEOUT_SEC)
    except ResourceBusyError as e:
        raise HTTPException(429, str(e))
    except ComfyUIError as e:
        # Lỗi từ ComfyUI (từ chối job / job lỗi / timeout) — trả 502 thay vì 500 khó hiểu.
        raise HTTPException(502, str(e))

    # Bước 6: lưu file + metadata vào gallery trước khi trả về. Bản cũ chỉ trả
    # base64 nên đóng trình duyệt là ảnh biến mất dù ComfyUI đã mất nhiều phút vẽ.
    import io
    image_id = uuid4().hex
    filename = f"{image_id}.png"
    output_path = Path(GENERATED_IMAGES_DIR) / filename
    img.save(output_path, format="PNG")
    image_row = await asyncio.to_thread(
        database.record_image,
        image_id=image_id,
        conversation_id=req.conversation_id,
        user_id=req.user_id,
        character_id=req.persona_id,
        file_path=f"images/{filename}",
        prompt=image_prompt,
        translated_prompt=en_tags,
        model_used=model_name,
        style=req.style,
        seed=seed,
        metadata={
            "quality": req.quality,
            "upscale": req.upscale,
            "translation_ok": translate_ok,
        },
    )
    if req.conversation_id:
        await asyncio.to_thread(
            database.append_message,
            req.conversation_id,
            "assistant",
            json.dumps(
                {
                    "image_id": image_id,
                    "image_url": f"/generated/{filename}",
                    "prompt": image_prompt,
                },
                ensure_ascii=False,
            ),
            content_type="image",
        )

    # Vẫn trả base64 để không làm hỏng client cũ; client mới dùng image_url nhẹ hơn.
    img_b64 = None
    if req.include_base64:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return {
        "image_id": image_row["id"],
        "image_url": f"/generated/{filename}",
        "image_base64": img_b64,
        "translated_prompt": en_tags,
        "original_prompt": image_prompt,
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
