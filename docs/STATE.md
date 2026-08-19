# Trạng thái dự án — YumeAI

**Cập nhật lần cuối:** 2026-08-18 · **Nhánh:** `review-fixes`

Trả lời đúng ba câu: đang chạy được gì, đang hỏng/thiếu gì, làm gì tiếp.
Cập nhật file này khi xong việc lớn hoặc phát hiện vấn đề lớn.

---

## ✅ Đang chạy được

**Trò chuyện**
- Chat streaming với nhân vật, chọn nhân vật bằng dropdown (Yuki / Hana)
- System prompt đầy đủ (~1810 ký tự) + few-shot 4–5 lượt mẫu → giữ vai tốt
- Lịch sử 12 lượt gần nhất + trí nhớ dài (nén các lượt cũ thành gạch đầu dòng)
- Hoạt động giống nhau ở cả Gradio và REST API

**Ảnh**
- text2img (SD 1.5) có ngoại hình nhân vật ghép vào prompt
- "Prompt thông minh" (LLM mở rộng mô tả) — bật/tắt được
- Giữ mặt/tư thế: ControlNet + IP-Adapter + ReActor
- Đổi đồ: Flux Kontext, mask+inpaint (SAM), garment transfer theo ảnh mẫu
- Xoá/thay vật thể bằng inpaint
- Upscale ESRGAN / Latent

**Hạ tầng**
- Định tuyến chat / tạo ảnh / sửa ảnh (rule + LLM fallback khi không chắc)
- Resource Guard: chỉ 1 job ComfyUI cùng lúc, có cả khoá liên tiến trình
- Degrade có kiểm soát khi thiếu Ollama/ComfyUI, thông báo lỗi có hướng dẫn
- `test_router.py` — 100+ test chạy offline, hiện **pass toàn bộ**
- pyflakes sạch trên toàn dự án

---

## ❌ Chưa có / đang hỏng

### Chặn hướng sản phẩm (ưu tiên cao nhất)

| Vấn đề | Chi tiết |
|---|---|
| **Không có database** | Đóng app là mất sạch hội thoại, trí nhớ, ảnh. Xem D8. |
| **Không có character consistency thật** | Ngoại hình đã ghép vào prompt, nhưng mỗi lần vẫn ra một người hơi khác. Cần LoRA riêng cho từng nhân vật hoặc IP-Adapter khoá mặt — **iGPU 4GB không train LoRA được**. |
| **Không có gallery** | Ảnh tạo xong không lưu, không xem lại được. |
| **Không có user** | Một người dùng ngầm định, không đăng nhập, không phân tách dữ liệu. |
| **Model chat quá nhỏ** | Mặc định `llama3.2:1b`. Nên đổi `qwen2.5:3b` (máy này ~12GB RAM) hoặc `qwen2.5:7b` nếu RAM rảnh. App có in cảnh báo lúc khởi động. |

### Nợ kỹ thuật đã biết

| Vấn đề | Chi tiết |
|---|---|
| `app_gradio.py` là khối 3800 dòng | Pipeline ảnh dính chặt Gradio → `main.py` không tái dùng được. REST API chỉ có chat + text2img. |
| `requirements.txt` không pin version | Rủi ro thật: file đang mang 3 miếng vá (Fix A/B/C) cho bug Content-Length của Gradio 6.x. |
| Không có test cho pipeline ảnh | `test_router.py` chỉ phủ router/lịch sử/preset/guard. Phần ảnh chỉ có 1 script thủ công (`test_auto_outfit.py`). |
| `env.example` khai key không dùng | `ANTHROPIC_API_KEY`, `FAL_KEY` — không code nào đọc. Xem D2. |
| `ComfyUI/` không trong git | 93GB model + 1.4GB custom_nodes phải copy tay khi chuyển máy. |

---

## 🎯 Việc tiếp theo, theo thứ tự

### 1. Database + trí nhớ bền vững ← **làm cái này trước**
SQLite. Bảng: `user`, `character`, `conversation`, `message`, `memory_fact`, `image`.
Nối vào `build_persona_messages(memory_summary=...)` — chỗ này đã thiết kế sẵn để
nhận trí nhớ từ ngoài, chỉ cần đọc từ DB thay vì tính lại mỗi phiên.
**Gần như không đụng GPU → làm được ngay trên máy hiện tại.**

### 2. Gallery ảnh
Lưu mọi ảnh sinh ra kèm metadata (nhân vật, prompt, seed, thời gian). Nối vào
`chat-ui-mockup.html`. Đây là thứ biến "công cụ" thành "sản phẩm".

### 3. Character consistency
Trước mắt trên máy hiện tại: seed cố định cho mỗi nhân vật + IP-Adapter khoá mặt
từ một ảnh gốc. Làm đầy đủ thì cần train LoRA → **phải có GPU rời**.

### 4. Dọn kiến trúc
Chuyển pipeline ảnh từ `app_gradio.py` sang `yumeai/pipelines/`, bỏ sạch `gr.*`
khỏi tầng core. `app_gradio.py` chỉ còn là client mỏng để debug.

### 5. Đổi model chat
```bash
ollama pull qwen2.5:3b     # hoặc qwen2.5:7b nếu RAM cho phép
```
Rồi đặt `OLLAMA_MODEL`. Đây là thay đổi rẻ nhất mà tác động lớn nhất tới chất
lượng hội thoại.

---

## 📌 Kỳ vọng cần giữ đúng

Model local 3–8B **sẽ không bằng Claude hay GPT** — đó là hàng trăm tỷ tham số
chạy trên datacenter. Với vai trò companion, thứ cần là **nhất quán và có cá
tính**, không phải **thông thái**. Mục tiêu đúng của phần persona/prompt là làm
nhân vật *không tuột vai* và *nhớ được người dùng*, chứ không phải làm nó thông
minh hơn.
