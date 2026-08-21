# Trạng thái dự án — YumeAI

**Cập nhật lần cuối:** 2026-08-21 · **Nhánh:** `review-fixes`

Trả lời đúng ba câu: đang chạy được gì, đang hỏng/thiếu gì, làm gì tiếp.
Cập nhật file này khi xong việc lớn hoặc phát hiện vấn đề lớn.

---

## ✅ Đang chạy được

**Trò chuyện**

- Chat streaming với persona Yuki/Hana, system prompt giàu chi tiết và few-shot.
- Lịch sử gần + tóm tắt phần cũ. Khoá cache summary băm đúng cửa sổ được gửi cho
  LLM, ổn định trong một lô `CHAT_SUMMARY_EVERY` và có cận trên
  `CHAT_SUMMARY_MAX_MESSAGES` (xem D13) — không dùng nhầm summary giữa hai
  conversation, cũng không gọi LLM lại mỗi lượt.
- Tóm tắt được ghi xuống `conversations.memory_summary` nên sống qua restart, và
  làm lưới đỡ khi Ollama chết.
- **Gradio và REST dùng chung một file SQLite** (D15): đóng Gradio mở lại, khung chat
  trống nhưng nhân vật vẫn nhớ chuyện cũ. Khối trí nhớ dựng bằng một hàm duy nhất
  `memory_service.build_conversation_memory()` nên hai cửa vào không lệch pha được.
- Gradio nay truyền `user_name`; trước đây chỉ REST truyền, nên qua Gradio nhân vật
  chưa bao giờ biết tên người dùng.
- Lỗi lưu trữ không giết cuộc trò chuyện: `_db_try()` in cảnh báo cụ thể rồi chạy tiếp.
- REST API lưu user message và assistant reply vào SQLite theo `conversation_id`.
- Đóng app/mở lại vẫn xem được lịch sử qua `http://127.0.0.1:8000/app`.
- Client cũ không có `conversation_id` vẫn dùng được trường `history` như trước.
- Fact extractor chạy nền mỗi 12 text message, tối đa 8 fact/context 2K; fact được
  dùng chung giữa các conversation của cùng user–persona và ghép vào system prompt.
- Web UI cho người dùng xem, thêm, sửa, xoá và refresh trí nhớ của nhân vật.

**Ảnh**

- text2img SD 1.5 có ngoại hình persona, smart prompt, preset quality/upscale.
- Pipeline Gradio vẫn có ControlNet, IP-Adapter, ReActor, Flux Kontext, SAM
  mask+inpaint, garment transfer và upscale.
- Ảnh tạo qua REST được lưu thành PNG cùng prompt, translated prompt, persona,
  model, style, seed và metadata; mở lại conversation vẫn hiển thị ảnh.
- `GET /api/gallery` trả gallery theo user/conversation.

**Dữ liệu và UI**

- SQLite schema **v3**: `users`, `characters`, `conversations`, `messages`,
  `memory_facts`, `images`; migration v1→v2→v3 giữ nguyên dữ liệu cũ và backfill
  chủ sở hữu cho ảnh (D14). Chỉ dùng thư viện chuẩn, không ORM.
- Web UI đọc sidebar/persona/history từ API thật; không còn mock response, Picsum,
  credit giả hay affection ngẫu nhiên.
- Dữ liệu mặc định tại `project/data/`, có thể đổi bằng `YUMEAI_DATA_DIR`.

**Kiểm chứng 2026-08-21**

Chạy được **trên cả máy Linux không có venv** (chỉ cần thư viện chuẩn):

- `python test_storage.py`: **12/12 pass** — gồm migration v1→v2→v3 trên DB có dữ
  liệu thật, và ca "ảnh không gắn hội thoại vẫn phải nằm trong gallery".
- `python test_chat_context.py`: **12/12 pass** — 7 test đếm số lời gọi LLM tóm tắt
  bằng module Ollama giả (cả 7 **fail trên code trước D13**, không phải test cho vui),
  5 test cho khối trí nhớ dùng chung hai cửa vào (D15).

Cần venv (máy Windows):

- `python test_router.py`: tất cả 100+ check pass.
- `python test_memory.py`: 3/3 pass, không gọi Ollama thật.
- `python test_api_storage.py`: 4/4 pass với Ollama giả.
- Uvicorn smoke: `/app`, `POST /api/conversations`, đọc lại conversation và
  `/api/gallery` đều HTTP 200.

Chung:

- pyflakes sạch trên `app_gradio.py`, `main.py`, `yumeai/` và toàn bộ test.
- Kiểu xuống dòng đúng: `main.py` + `prompt_router.py` CRLF, còn lại LF.
- JavaScript trong `chat-ui-mockup.html` parse thành công.

**Số đo sau D13** (60 lượt chat): lời gọi LLM tóm tắt **54 → 18**; payload gửi vào
LLM từ không giới hạn → tối đa 40 message.

---

## ❌ Chưa có / đang hỏng

### Chặn hướng sản phẩm

| Vấn đề | Chi tiết |
|---|---|
| **Chưa có relevance retrieval** | Fact extraction đã chạy, nhưng hiện đưa tối đa 24 fact gần nhất vào prompt. Chỉ cần FTS5/embedding khi dữ liệu thực tế đủ lớn để benchmark. |
| **Character consistency chưa đủ** | Appearance tag chưa khoá được đúng một khuôn mặt. Cần ảnh gốc + seed/preset IP-Adapter trước; LoRA cần GPU rời. |
| **Chưa có authentication** | Dữ liệu phân tách bằng `user_id`, nhưng đây chưa phải đăng nhập. An toàn khi chỉ bind localhost; không được mở Internet như hiện tại. |
| **Model chat mặc định quá nhỏ** | `llama3.2:1b` giữ vai kém. Tối thiểu nên dùng model 3B phù hợp RAM sau khi benchmark trên máy thật. |

### Nợ kỹ thuật đã biết

| Vấn đề | Chi tiết |
|---|---|
| `app_gradio.py` là khối ~3800 dòng | Pipeline ảnh dính Gradio; REST mới dùng chung chat + text2img, chưa dùng được các edit pipeline nâng cao. |
| `requirements.txt` chưa pin version | Có rủi ro lệch Gradio/FastAPI giữa hai máy. |
| Chưa có migration runner | Schema hiện ở v3, migrate bằng cách kiểm `PRAGMA table_info` rồi `ALTER TABLE` — idempotent và đã có test v1→v2→v3, nhưng vẫn chưa phải runner tuần tự thật. |
| Tóm tắt dùng cửa sổ trôi | Sau ~40 message overflow, fact từ đầu hội thoại rơi khỏi tóm tắt (D13). Đường bền vững là `memory_facts` — nay đã có ở **cả hai** cửa vào (D15). Làm đúng hẳn cần tóm tắt cuộn (summary cũ + phần mới → summary mới). |
| Ảnh từ Gradio chưa vào gallery | Bảng `images` mới chỉ được ghi ở đường REST. Cần hook ở `runtime.run_comfy_job` — điểm nghẽn duy nhất mà cả 8 chỗ gọi đều đi qua (D15). |
| Chưa test ảnh tự động | Test mới phủ storage/API; ComfyUI workflow vẫn cần service/model thật. |
| `ComfyUI/` không trong git | 93GB model + custom nodes phải copy/cài riêng khi chuyển máy. |

---

## 🎯 Việc tiếp theo, theo thứ tự

### 1. Đánh giá chất lượng memory trên hội thoại thật

Chạy tối thiểu 20–30 lượt thật với model local để đo tỷ lệ fact đúng/sai/trùng,
thời gian background extraction và tác động RAM. Chỉ thêm FTS5/embedding nếu 24
fact gần nhất làm prompt nhiễu hoặc bỏ sót fact liên quan.

### 2. Character consistency khả thi trên máy hiện tại

Mỗi persona có ảnh gốc, seed và preset IP-Adapter/ReActor riêng. Đo 20 ảnh để so
sánh trước/sau. LoRA hoãn tới khi có GPU rời đủ VRAM.

### 3. Tách pipeline ảnh khỏi Gradio

Chuyển pipeline từ `app_gradio.py` sang `yumeai/pipelines/`; cả `/app` và Gradio
gọi chung. Khi đó REST mới có đổi đồ/inpaint/giữ mặt đầy đủ.

### 4. Hoàn thiện beta

Gallery dạng lưới, export/delete dữ liệu, backup/restore, age gate và AI disclosure.
Chỉ thêm auth/rate-limit/CORS chặt khi có quyết định mở ngoài localhost.

### 5. Benchmark model chat

So sánh model 3B/4B local đang có trên đúng máy này bằng cùng bộ hội thoại persona,
đo RAM, token/giây, độ giữ vai và tiếng Việt; không chọn chỉ theo tên model.

Chi tiết: [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md).

---

## 📌 Kỳ vọng cần giữ đúng

Model local nhỏ không cần trở thành trợ lý thông thái. Với companion, ưu tiên là
giữ vai, nhớ đúng, phản hồi tự nhiên và nhất quán hình ảnh. Mọi tính năng mới phải
được đánh giá theo bốn tiêu chí đó và giới hạn phần cứng Radeon 680M.
