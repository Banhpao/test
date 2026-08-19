# Nhật ký thay đổi — YumeAI

Mục mới nhất ở **trên cùng**. Ghi nhanh một dòng bằng:

```bash
cd project && python devlog.py "Mô tả ngắn thay đổi"
```

Quyết định thiết kế thì ghi vào [`DECISIONS.md`](DECISIONS.md), không ghi ở đây.

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
