# main.py
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import asyncio

app = FastAPI()

# Cho phép frontend gọi API (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic()  # cần ANTHROPIC_API_KEY trong .env

class ChatRequest(BaseModel):
    message: str
    persona_id: str
    user_id: str

@app.post("/api/chat")
async def chat(req: ChatRequest):
    # System prompt của persona (sau này load từ DB)
    system = """Bạn là Yuki, 22 tuổi, sinh viên nghệ thuật ở Tokyo.
    Tính cách tsundere — hay phủ nhận nhưng thực ra rất quan tâm.
    Hay dùng "Baka!", "Không phải tao thích mày đâu nhé".
    Trả lời ngắn gọn, tự nhiên, bằng tiếng Việt."""

    # Stream response về frontend
    def generate():
        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": req.message}]
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {text}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")