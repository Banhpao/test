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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask
import httpx, json, asyncio, base64, random, threading, time
import os, tempfile, zipfile
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from yumeai.core.config import (
    OLLAMA_URL, COMFY_URL, OLLAMA_MODEL, COMFY_TIMEOUT_SEC, CHAT_TIMEOUT_SEC,
    DATABASE_PATH, GENERATED_IMAGES_DIR, MEMORY_EXTRACTION_EVERY_MESSAGES,
    MEMORY_FACTS_ENABLED, OLLAMA_NUM_GPU,
)
from yumeai.core.personas import PERSONA_ID_ALIASES, PERSONAS, resolve_persona_id
# Lịch sử chat dùng CHUNG với app_gradio.py — trước đây endpoint này không có
# lịch sử gì cả nên nhân vật mất trí nhớ sau mỗi câu.
from yumeai.core.chat_context import (
    build_persona_messages,
    chat_options,
    history_load_limit,
)
# Prompt text2img dùng CHUNG với app_gradio.py — hai cửa vào phải cho ra cùng một ảnh
from yumeai.core.image_prompt import build_positive_prompt, NEGATIVE_PROMPT
# Bộ định tuyến chat ↔ tạo ảnh dùng CHUNG với app_gradio.py.
from yumeai.core.prompt_router import plan_request
from yumeai.core.runtime import run_comfy_job, ResourceBusyError
# Số đo thật mỗi lượt chat / mỗi ảnh (chỉ số, không nội dung) → data/perf.jsonl, /api/stats.
from yumeai.core.perf import ollama_chunk_stats, perf_log
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
    build_conversation_memory,
    refresh_conversation_memory,
)
from yumeai.storage import YumeDatabase

Path(GENERATED_IMAGES_DIR).mkdir(parents=True, exist_ok=True)
database = YumeDatabase(DATABASE_PATH)
WEB_UI_PATH = Path(__file__).with_name("chat-ui-mockup.html")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Khởi tạo DB trước khi nhận request và warmup model ở background."""
    await asyncio.to_thread(database.initialize, PERSONAS, PERSONA_ID_ALIASES)
    warn = check_chat_model()
    if warn:
        print(warn)
    threading.Thread(target=warmup_ollama, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
# KHÔNG có CORSMiddleware, có chủ đích. Bản cũ đặt allow_origins=["*"] trong khi API
# không có đăng nhập: bind 127.0.0.1 không chặn được một trang web BẤT KỲ đang mở
# trong cùng trình duyệt gọi http://127.0.0.1:8000/api/conversations, /api/memory,
# /api/gallery — và wildcard CORS cho trang đó đọc được hết. UI thật phục vụ ở /app
# (cùng origin) nên không cần CORS. Mở file HTML trực tiếp (file://) không còn gọi
# được API — hãy mở http://127.0.0.1:8000/app.
app.mount(
    "/generated",
    StaticFiles(directory=GENERATED_IMAGES_DIR),
    name="generated-images",
)


class _PersonaRef(BaseModel):
    """persona_id dùng chung cho mọi request.

    Nhận cả id cũ của nhân vật đã đổi tên (vd. "yuki" trước D18) rồi đổi sang id
    hiện hành, để tab /app mở từ trước hay script cũ không bị 404.
    """
    persona_id: str = "pao"

    @field_validator("persona_id")
    @classmethod
    def _current_persona_id(cls, value: str) -> str:
        return resolve_persona_id(value)


class ChatReq(_PersonaRef):
    message: str
    user_id: str = "user_001"
    user_name: str = ""   # tên người dùng, để nhân vật gọi đúng
    conversation_id: str | None = None
    # Giữ để tương thích client cũ. Khi có conversation_id, server luôn đọc lịch
    # sử thật từ SQLite và bỏ qua trường này.
    history: list = Field(default_factory=list)


class ImgReq(_PersonaRef):
    prompt: str
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
    # Câu người dùng gõ trong khung chat để xin ảnh ("vẽ cho anh một tấm..."). Có thì
    # lưu thành lượt user ngay trước ảnh, để mở lại hội thoại vẫn đọc được ai xin gì.
    # Chỉ lưu khi vẽ xong — giống /api/chat chỉ lưu lượt chat khi đã có câu trả lời.
    chat_message: str = ""


class ChatIntentReq(_PersonaRef):
    message: str
    style: str = "Anime"


class DataDeleteReq(BaseModel):
    user_id: str = "user_001"
    # Phải gõ đúng chữ này: xoá toàn bộ dữ liệu không hoàn tác được.
    confirm: str = ""


class ConversationCreateReq(_PersonaRef):
    user_id: str = "user_001"
    user_name: str = ""
    title: str = ""


class MemoryFactReq(_PersonaRef):
    user_id: str = "user_001"
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
            "chat_intent": "POST /api/chat/intent",
            "image": "POST /api/images/generate",
            "gallery": "/api/gallery",
            "memory": "/api/memory",
            "export": "/api/data/export",
            "stats": "/api/stats",
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


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, user_id: str = "user_001"):
    """Xoá hội thoại và tin nhắn của nó; ảnh trong đó vẫn ở lại bộ sưu tập (D14)."""
    await _owned_conversation(conversation_id, user_id)
    await asyncio.to_thread(database.delete_conversation, conversation_id)
    return {"deleted": True, "conversation_id": conversation_id}


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


def _image_file(file_path: str) -> Path:
    # Chỉ lấy tên file: file_path trong DB là "images/<id>.png"; không để một giá trị lạ
    # trỏ ra ngoài thư mục ảnh.
    return Path(GENERATED_IMAGES_DIR) / Path(file_path).name


def _remove_image_files(file_paths) -> int:
    removed = 0
    for file_path in file_paths:
        try:
            _image_file(file_path).unlink(missing_ok=True)
            removed += 1
        except OSError as e:
            print(f"⚠️ Không xoá được file ảnh {file_path}: {e}")
    return removed


@app.delete("/api/gallery/{image_id}")
async def delete_gallery_image(image_id: str, user_id: str = "user_001"):
    """Xoá một ảnh: dòng trong DB, file PNG, và tin nhắn ảnh trong hội thoại chứa nó."""
    image = await asyncio.to_thread(database.get_image, image_id)
    # Ảnh user_id = '' là ảnh cũ chưa rõ chủ — gallery vẫn cho mọi người xem nên cũng
    # cho xoá. Ảnh của người khác thì trả 404, không làm lộ là ảnh đó có tồn tại.
    if not image or image["user_id"] not in (user_id, ""):
        raise HTTPException(404, "Không tìm thấy ảnh")
    deleted = await asyncio.to_thread(database.delete_image, image_id)
    await asyncio.to_thread(_remove_image_files, [image["file_path"]])
    return {
        "deleted": True,
        "image_id": image_id,
        "messages_removed": (deleted or {}).get("messages_removed", 0),
    }


# ══════════════════════════════════
# DỮ LIỆU CỦA BẠN — xuất và xoá toàn bộ (P3)
# ══════════════════════════════════
def _build_export_zip(user_id: str) -> str:
    data = database.export_user(user_id)
    fd, zip_path = tempfile.mkstemp(prefix="yumeai-export-", suffix=".zip")
    os.close(fd)
    missing = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for image in data["images"]:
            source = _image_file(image["file_path"])
            if source.exists():
                # PNG đã nén sẵn, nén thêm chỉ tốn thời gian.
                zf.write(source, f"images/{source.name}", compress_type=zipfile.ZIP_STORED)
            else:
                missing.append(image["file_path"])
        payload = {
            "app": "YumeAI",
            "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "user_id": user_id,
            "missing_image_files": missing,
            **data,
        }
        zf.writestr("data.json", json.dumps(payload, ensure_ascii=False, indent=2))
    return zip_path


@app.get("/api/data/export")
async def export_data(user_id: str = "user_001"):
    """Tải về một file zip: data.json (hội thoại, tin nhắn, ký ức, thông tin ảnh) + ảnh."""
    zip_path = await asyncio.to_thread(_build_export_zip, user_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"yumeai-du-lieu-{stamp}.zip",
        background=BackgroundTask(os.remove, zip_path),
    )


@app.post("/api/data/delete")
async def delete_all_data(req: DataDeleteReq):
    """Xoá toàn bộ dữ liệu của người dùng. Không hoàn tác, không tự sao lưu.

    Nhân vật (bảng characters) giữ nguyên. Bản sao lưu cũ trong data/backups không bị
    đụng tới — giao diện nói rõ điều đó để người dùng tự xoá nếu muốn.
    Sổ đo hiệu năng (perf.jsonl) cũng bị xoá: không có nội dung nhưng có giờ từng lượt
    chat/vẽ. Sổ không chia theo người dùng nên xoá cả sổ (app local một người dùng).
    """
    if req.confirm.strip().upper() not in ("XOÁ", "XÓA", "XOA"):
        raise HTTPException(400, 'Gõ chữ "XOÁ" để xác nhận xoá toàn bộ dữ liệu')
    result = await asyncio.to_thread(database.delete_user_data, req.user_id)
    files_removed = await asyncio.to_thread(_remove_image_files, result.pop("image_files"))
    perf_files_removed = await asyncio.to_thread(perf_log.clear)
    return {
        "deleted": True,
        **result,
        "image_files_removed": files_removed,
        "perf_files_removed": perf_files_removed,
    }


# ══════════════════════════════════
# MEMORY FACTS — trí nhớ có thể xem và sửa từ UI
# ══════════════════════════════════
@app.get("/api/memory")
async def list_memory(
    user_id: str = "user_001",
    persona_id: str = "pao",
):
    persona_id = resolve_persona_id(persona_id)
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
    persona_id: str = "pao",
):
    persona_id = resolve_persona_id(persona_id)
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
# STATS — số đo hiệu năng thật trên máy này (lộ trình P3.5)
# ══════════════════════════════════
@app.get("/api/stats")
async def stats():
    """Trung vị token/giây, thời gian chờ chữ đầu, số phút mỗi preset ảnh — đọc từ perf.jsonl.

    File đó chỉ có số và tên model/preset, không có nội dung hay user_id (xem
    yumeai/core/perf.py), nên endpoint này không lọc theo người dùng.
    """
    return await asyncio.to_thread(perf_log.summarize)


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
    # Mốc tính "người dùng phải chờ bao lâu": từ lúc nhận tin nhắn, gồm cả phần dựng trí nhớ.
    started = time.perf_counter()

    history = req.history
    conversation = None
    memory_context = None
    if req.conversation_id:
        conversation = await _owned_conversation(
            req.conversation_id, req.user_id, req.persona_id
        )
        # Chỉ đọc phần lịch sử ngữ cảnh thật sự dùng (cửa sổ gần + cửa sổ tóm tắt),
        # không đọc lại cả hội thoại mỗi lượt — xem history_load_limit().
        total_text = await asyncio.to_thread(
            database.count_messages, req.conversation_id, content_type="text"
        )
        stored_messages = await asyncio.to_thread(
            database.list_messages,
            req.conversation_id,
            history_load_limit(total_text),
            content_type="text",
        )
        # Tin nhắn ảnh có JSON để UI dựng lại gallery; không đưa JSON thô đó vào
        # prompt vì model sẽ bắt chước cấu trúc máy móc.
        history = [
            {"role": item["role"], "content": item["content"]}
            for item in stored_messages
            if item["content_type"] == "text"
        ]
        # KHÔNG ghi tin nhắn người dùng ở đây nữa: bản cũ ghi trước khi gọi LLM, nên
        # Ollama lỗi/timeout hay đóng tab giữa chừng là để lại một lượt user mồ côi
        # (hai lượt user liền nhau trong prompt), và bấm gửi lại là nhân đôi câu đó.
        # Nay ghi CÙNG LÚC với câu trả lời, khi stream đã xong (xem stream()).

        # Dựng trí nhớ bằng hàm DÙNG CHUNG với app_gradio.py — không viết lại logic
        # ngữ cảnh ở entrypoint, đó đúng là chỗ dự án từng lệch pha (D6).
        # Có thể gọi LLM tóm tắt nên phải rời khỏi event loop.
        memory_context = await asyncio.to_thread(
            build_conversation_memory,
            database,
            conversation,
            history,
            persona.get("name", ""),
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
    prompt_ready = time.perf_counter()

    def record_chat(status, first_token_at, ollama_stats, error):
        # Chỉ ghi số (xem yumeai/core/perf.py) — không ghi tin nhắn hay câu trả lời.
        perf_log.record(
            "chat",
            status=status,
            model=OLLAMA_MODEL,
            persona=req.persona_id,
            num_gpu=OLLAMA_NUM_GPU,
            prep_s=round(prompt_ready - started, 3),
            first_token_s=None if first_token_at is None else round(first_token_at - started, 3),
            total_s=round(time.perf_counter() - started, 3),
            error=error,
            **ollama_stats,
        )

    async def stream():
        reply_parts = []
        # "cancelled" là mặc định: người dùng đóng tab giữa chừng thì generator bị đóng ở
        # một yield bất kỳ và chỉ còn khối finally chạy.
        status, error, first_token_at, ollama_stats = "cancelled", None, None, {}
        try:
            async with httpx.AsyncClient(timeout=CHAT_TIMEOUT_SEC) as c:
                try:
                    async with c.stream("POST", f"{OLLAMA_URL}/api/chat", json={
                        "model": OLLAMA_MODEL,
                        "messages": messages,
                        "stream": True,
                        # Model biết "suy nghĩ" sẽ tiêu hết num_predict vào phần suy luận.
                        "think": False,
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
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                reply_parts.append(tok)
                                payload = json.dumps({"token": tok}, ensure_ascii=False)
                                yield f"data: {payload}\n\n"
                            if d.get("done"):
                                ollama_stats = ollama_chunk_stats(d)
                                full_reply = "".join(reply_parts).strip()
                                if req.conversation_id and full_reply:
                                    await asyncio.to_thread(
                                        database.append_message,
                                        req.conversation_id,
                                        "user",
                                        user_message,
                                    )
                                    await asyncio.to_thread(
                                        database.append_message,
                                        req.conversation_id,
                                        "assistant",
                                        full_reply,
                                    )
                                status = "ok" if full_reply else "empty"
                                completion["done"] = True
                                yield 'data: {"done":true}\n\n'
                                return
                    # Ollama đóng kết nối mà chưa gửi chunk done.
                    status = "incomplete"
                except Exception as e:
                    # Chỉ ghi TÊN lỗi vào số đo; nội dung lỗi vẫn gửi cho client như cũ.
                    status, error = "error", type(e).__name__
                    payload = json.dumps({"error": str(e)}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"
        finally:
            record_chat(status, first_token_at, ollama_stats, error)

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
# CHAT INTENT — tin nhắn này là trò chuyện hay xin ảnh?
# ══════════════════════════════════
@app.post("/api/chat/intent")
async def chat_intent(req: ChatIntentReq):
    """Dùng chung plan_request() với Gradio để /app biết khi nào nhân vật nên vẽ.

    REST chưa nhận ảnh đính kèm nên chỉ có hai đáp án: "chat" hoặc "text2img". Các
    task sửa ảnh (inpaint, đổi đồ...) đều cần ảnh đầu vào nên quy về "chat".
    Rule-based quyết gần như mọi câu ngay lập tức; chỉ câu có nhắc tới ảnh/hình mà
    không rõ ý mới hỏi thêm LLM (xem prompt_router.rule_plan_request).
    """
    if req.persona_id not in PERSONAS:
        raise HTTPException(404, "Persona không tồn tại")
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Tin nhắn không được để trống")
    plan = await asyncio.to_thread(
        plan_request,
        text=message,
        has_image=False,
        has_outfit_image=False,
        default_style=req.style,
        use_llm_fallback=True,
    )
    task = "text2img" if plan.get("task") == "text2img" else "chat"
    return {
        "task": task,
        "style": plan.get("style") or req.style,
        "reason": plan.get("reason", ""),
    }


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

    started = time.perf_counter()
    timing = {}

    def record_image(status, *, model=None, size=None, translation_ok=None, error=None):
        # Chỉ ghi số và preset (xem yumeai/core/perf.py) — không ghi prompt.
        timing["total_s"] = round(time.perf_counter() - started, 3)
        perf_log.record(
            "image",
            status=status,
            style=req.style,
            quality=req.quality,
            upscale=req.upscale,
            enrich=req.enrich,
            from_chat=bool(req.chat_message.strip()),
            model=model,
            width=size[0] if size else None,
            height=size[1] if size else None,
            translation_ok=translation_ok,
            error=error,
            **timing,
        )

    # Bước 1: Dịch tiếng Việt → EN (dùng chung ollama_service, không tự viết lại).
    # translate_ok=False nghĩa là Ollama không phản hồi và en_tags VẪN LÀ tiếng Việt —
    # Stable Diffusion không hiểu tiếng Việt nên ảnh sẽ rác. Phải báo cho client biết,
    # bản cũ nuốt lỗi im lặng.
    if req.enrich:
        en_tags, translate_ok = await asyncio.to_thread(enrich_for_create_ex, image_prompt, req.style)
    else:
        en_tags, translate_ok = await asyncio.to_thread(translate_vi_en_ex, image_prompt)
    timing["translate_s"] = round(time.perf_counter() - started, 3)

    # Bước 2: Lấy model checkpoint hợp lệ (loại SDXL/LoRA/inpaint — máy 4GB VRAM).
    # get_checkpoint_model nay ném ComfyUIError kèm hướng dẫn cụ thể khi ComfyUI chưa
    # bật, thay vì trả "" rồi báo "Không tìm thấy model checkpoint" gây hiểu nhầm.
    try:
        model_name = await asyncio.to_thread(get_checkpoint_model, req.style)
    except ComfyUIError as e:
        record_image("error", translation_ok=translate_ok, error="checkpoint")
        raise HTTPException(502, str(e))
    if not model_name:
        record_image("error", translation_ok=translate_ok, error="no_checkpoint")
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
    comfy_started = time.perf_counter()
    try:
        # run_comfy_job giữ slot trong MỘT khối: submit và poll không còn tách đôi
        # ở hai hàm khác nhau nữa (xem comfy_job() trong yumeai/core/runtime.py).
        img = await asyncio.to_thread(run_comfy_job, workflow, None, 0.0, COMFY_TIMEOUT_SEC)
    except ResourceBusyError as e:
        record_image("busy", model=model_name, translation_ok=translate_ok)
        raise HTTPException(429, str(e))
    except ComfyUIError as e:
        timing["comfy_s"] = round(time.perf_counter() - comfy_started, 3)
        record_image("error", model=model_name, translation_ok=translate_ok, error="comfy")
        # Lỗi từ ComfyUI (từ chối job / job lỗi / timeout) — trả 502 thay vì 500 khó hiểu.
        raise HTTPException(502, str(e))
    # Gồm cả bước nhường VRAM của Ollama (D16) trong run_comfy_job — đúng là phần người dùng chờ.
    timing["comfy_s"] = round(time.perf_counter() - comfy_started, 3)

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
        chat_message = req.chat_message.strip()
        if chat_message:
            await asyncio.to_thread(
                database.append_message,
                req.conversation_id,
                "user",
                chat_message,
            )
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

    record_image("ok", model=model_name, size=img.size, translation_ok=translate_ok)

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
        # Giây: dịch/viết prompt, ComfyUI vẽ, cả lượt. Client cũ bỏ qua trường mới này.
        "timing": dict(timing),
        "warning": None if translate_ok else (
            "Không dịch được sang tiếng Anh (Ollama không phản hồi). Prompt tiếng Việt "
            "đã đi thẳng vào Stable Diffusion nên ảnh có thể sai hoặc rác."
        ),
    }


if __name__ == "__main__":
    import uvicorn
    # host 127.0.0.1 để browser truy cập được, port 8000
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
