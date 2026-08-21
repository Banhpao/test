# Nhật ký thay đổi — YumeAI

Mục mới nhất ở **trên cùng**. Ghi nhanh một dòng bằng:

```bash
cd project && python devlog.py "Mô tả ngắn thay đổi"
```

Quyết định thiết kế thì ghi vào [`DECISIONS.md`](DECISIONS.md), không ghi ở đây.

---

## 2026-08-21

- Nối Gradio vào cùng SQLite với REST (D15): lịch sử prompt đọc từ DB nên mở lại app nhân vật vẫn nhớ; Gradio nay truyền user_name (trước đây chưa bao giờ có); khối trí nhớ gom vào memory_service.build_conversation_memory() dùng chung hai cửa vào, main.py bỏ bản copy; lỗi DB không giết chat (_db_try). Thêm get_or_create_conversation + 9 test, tổng 24 test offline.

---

## 2026-08-21

- Sửa 3 lỗi tầng trí nhớ/gallery: khoá cache tóm tắt băm đúng cửa sổ gửi cho LLM (54->18 lời gọi/60 lượt, D13), summarize_conversation cắt từ đầu thay vì bỏ phần mới, ghi memory_summary xuống DB, images.user_id thay INNER JOIN (D14). Thêm test_chat_context.py 7/7 + 3 test storage, tất cả chạy offline.

---

## 2026-08-21 — SQLite persistence + web companion UI

### Fact memory chạy nền cho máy yếu

- Nâng SQLite schema v1 → v2 không mất dữ liệu; thêm con trỏ
  `memory_processed_message_id` để mỗi batch chỉ xử lý message mới.
- Thêm `memory_service.py`: sau mỗi 12 text message, dùng model Ollama đang có với
  context 2K để trích tối đa 8 fact; chạy sau SSE nên không chặn câu trả lời.
- Lỗi extraction có cooldown 5 phút; batch không có fact vẫn được đánh dấu đã xem,
  tránh gọi LLM lặp vô hạn trên máy yếu.
- Không tự lưu mật khẩu, OTP, API key, tài khoản ngân hàng, giấy tờ định danh hoặc
  địa chỉ chính xác.
- Memory fact được ghép vào system prompt và dùng chung giữa các conversation của
  cùng user–persona.
- Thêm `GET/PUT/DELETE /api/memory`; web UI có panel xem, thêm, sửa, xoá và refresh
  trí nhớ của nhân vật.
- Test mới: storage 7/7, memory 3/3, API 4/4; router suite và pyflakes vẫn pass.

- Thêm SQLite schema/repository cho user, character, conversation, message,
  memory fact và image metadata; dữ liệu mặc định ở `project/data/`.
- Thêm REST API tạo/liệt kê/mở conversation, đọc message và gallery.
- `POST /api/chat` nhận `conversation_id`, đọc history từ DB, stream SSE dạng JSON
  và lưu assistant reply khi hoàn tất; client cũ vẫn dùng `history` được.
- Ảnh sinh qua REST được lưu PNG + prompt/model/style/seed/metadata, trả thêm
  `image_url` và xuất hiện lại trong conversation sau khi mở app.
- Biến `chat-ui-mockup.html` thành UI thật tại `/app`: sidebar/history/persona đọc
  từ API; bỏ mock response, Picsum, credit và affection ngẫu nhiên.
- Sửa cache memory summary có thể dùng nhầm giữa hai conversation cùng persona.
- Thay FastAPI startup event deprecated bằng lifespan handler.
- Dọn `env.example`, thêm cấu hình data path và hướng dẫn backup/runbook.
- Thêm `test_storage.py` (5 test) và `test_api_storage.py` (3 test); router suite,
  pyflakes, JavaScript parse và Uvicorn HTTP smoke đều pass.

## 2026-08-20

- Tách dự án Yume Assistant (trợ lý kỹ thuật, RAG + tool + ROS2 sau này) sang repo riêng ../yume_assistant, copy 3 module lõi. Xem AGENTS.md của repo đó.

---

## 2026-08-18

- Thêm bộ nhớ dự án: AGENTS.md, CLAUDE.md, docs/ (STATE, DECISIONS, CHANGELOG, RUNBOOK) và devlog.py

---

## 2026-08-18 — Rà soát toàn hệ thống + nâng cấp chất lượng hội thoại

Một phiên làm việc dài, chia làm 5 đợt. Nhánh `review-fixes`.

### Đợt 1 — Sửa router, lịch sử chat, persona

- **Router mặc định đổi sang `chat`** thay vì `text2img`. Trước đó
  `'hôm nay bạn thế nào?'`, `'anh nhớ em không'`, `'em ăn cơm chưa'` đều bị đem đi
  vẽ tranh. Xem D4.
- **Thêm `contains_word()`** khớp theo ranh giới từ. Sửa `"hi"` khớp trong "khi/chi/
  hiểu/nhiều" và `"đồ"` khớp trong "đồng ý/đồng phục". Xem D5.
- Thêm trường `confidence` vào `PromptPlan`; LLM fallback nay chạy theo độ tin cậy
  chứ không theo tên task. Planner gọi Ollama với `"format": "json"`.
- **File mới `yumeai/core/chat_context.py`** — xử lý lịch sử dùng chung. Bỏ bộ lọc
  emoji làm mất trắng mọi lượt nói có kèm ảnh; thêm giới hạn cửa sổ lịch sử.
- **Thêm dropdown chọn nhân vật** trong UI Gradio (trước khoá cứng vào Yuki).
- Sửa bug: chat khi đang có ảnh bị ném `gr.Error("Pipeline đã chạy nhưng không trả
  về ảnh mới")` vì `task="chat"` luôn trả `image=None`.
- `main.py`: nhận `history`, warmup Ollama lúc startup, thêm `GET /api/personas`.
- Bỏ hardcode `D:\baitap\...` trong `config.py`.
- **File mới `test_router.py`** — bộ test chạy offline.

### Đợt 2 — Xử lý khi thiếu dịch vụ

- `comfy_is_up()` / `ensure_comfy_ready()` — báo "ComfyUI chưa chạy" kèm hướng dẫn
  thay vì "Không tìm thấy model!" gây hiểu nhầm.
- Vòng poll bỏ cuộc sau 5 lần mất kết nối liên tiếp thay vì chờ đủ 30 phút timeout.
- `translate_vi_en_ex()` trả `(text, ok)` — dịch hỏng nay hiện cảnh báo rõ ràng
  thay vì âm thầm đưa tiếng Việt vào Stable Diffusion.
- `get_translate_model()` dò lười, chỉ cache khi thật sự hỏi được Ollama. Bỏ hằng
  `TRANSLATE_MODEL` (chốt giá trị lúc import chính là bug).
- Bỏ hardcode path trong `fix_dino.py`.

### Đợt 3 — Dọn desync + hiệu năng

- **`comfy_job()` / `run_comfy_job()`** trong `runtime.py` — gộp submit và poll vào
  một khối có khoá. Trước đó lấy khoá ở hàm này nhả ở hàm kia, exception chen giữa
  là kẹt vĩnh viễn. Đã chuyển 7 call site trong `app_gradio.py` + 1 trong `main.py`.
- **Cache vision** theo vân tay ảnh (md5 64×64). `choose_best_edit_mode()` không
  còn gọi vision khi `mode == "region"`. Một lượt đổi đồ 2 nhân vật: ~8 → ~4 lần
  gọi vision.
- **`normalize_upscale()` / `normalize_quality()`** — quy mọi cách viết về một mối,
  giá trị lạ thì cảnh báo thay vì im lặng bỏ qua.
- **File mới `yumeai/core/image_prompt.py`** — prompt text2img dùng chung. Hai
  entrypoint nay cho cùng một ảnh với cùng input. Xem D6.
- **Nối checkbox "🧠 Prompt thông minh"** — trước đó nó không được nối vào đâu cả,
  bật hay tắt đều như nhau.
- Bỏ import chết (`uuid`, `time`, `ImageFilter`, `numpy`, `tempfile`). Toàn dự án
  pyflakes sạch.

### Đợt 4 — Chất lượng hội thoại

- **Persona 2.0**: hồ sơ đầy đủ (`personality`, `speech_style`, `quirks`, `likes`/
  `dislikes`, `boundaries`, `greeting`, `examples`). System prompt 230 → ~1810 ký
  tự. Xem D7.
- **File mới `yumeai/core/persona_prompt.py`** — dựng system prompt + few-shot.
- **Trí nhớ dài**: `summarize_old_history()` nén các lượt rơi khỏi cửa sổ thành
  gạch đầu dòng, nhét vào system prompt. Tóm tắt lại mỗi 6 message thay vì mỗi lượt.
- Tham số sinh câu: thêm `top_p=0.92`, `repeat_penalty=1.12` (model nhỏ hay lặp câu).
- `check_chat_model()` — cảnh báo lúc khởi động nếu đang dùng model quá nhỏ.
- `main.py`: thêm `user_name`, `style`, `enrich` vào request; `/api/personas` trả
  đầy đủ thông tin nhân vật.

### Đợt 5 — Bộ nhớ dự án

- Thêm `AGENTS.md`, `CLAUDE.md`, `docs/` (STATE, DECISIONS, CHANGELOG, RUNBOOK) và
  `devlog.py`. Xem D10.

### Phát hiện về phần cứng

GPU là **AMD Radeon 680M** — iGPU tích hợp, không có VRAM riêng. "4GB VRAM" là RAM
hệ thống bị cắt ra (UMA Frame Buffer), nên hệ điều hành chỉ thấy ~12.4GB trên máy
16GB. Hệ quả: nâng RAM **không** làm ComfyUI nhanh hơn; cần GPU rời. Xem D3.

---

## Trước 2026-08-18

Lịch sử git (5 commit):

| Commit | Nội dung |
|---|---|
| `7aca2ef` | Dọn dẹp + sửa lỗi sau review toàn hệ thống YumeAI |
| `7c5ff5d` | Update new features |
| `7f45eb7` | Update new features |
| `b2e83df` | Update new features |
| `69cd8df` | Initial commit |

Giai đoạn này xây phần lớn pipeline ảnh trong `app_gradio.py`: ControlNet,
IP-Adapter, ReActor, Flux Kontext, SAM mask+inpaint, garment transfer, pipeline
2-pass nhiều nhân vật, teacher plan + verify + rollback (các lớp đánh dấu V7 →
V8.16 trong code). Thông điệp commit không mô tả chi tiết — nếu cần hiểu một lớp
Vx cụ thể thì đọc thẳng docstring trong `app_gradio.py`.
