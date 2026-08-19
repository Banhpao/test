# Runbook — YumeAI

Lệnh vận hành và xử lý sự cố. Thay cho file ghi chú `project/text` cũ.

---

## Khởi động (cần 3 tiến trình, mở 3 cửa sổ riêng)

### 1. Ollama — BẮT BUỘC (chat, dịch, vision)
```bash
ollama serve
```
Kiểm tra: `ollama list` phải thấy model chat, model dịch và model vision.

Model cần có:
| Vai trò | Model | Ghi chú |
|---|---|---|
| Chat | `qwen2.5:3b` trở lên | `llama3.2:1b` quá nhỏ, app sẽ cảnh báo |
| Dịch VI→EN | `qwen2.5:3b` | tự dò, ưu tiên qwen2.5 |
| Vision | `qwen2.5vl` | mô tả ảnh cho pipeline đổi đồ |

### 2. ComfyUI — BẮT BUỘC nếu muốn có ảnh
```bash
cd project/ComfyUI
python main.py --lowvram
```
Đợi dòng `To see the GUI go to: http://127.0.0.1:8188`.

### 3. Cửa vào — chọn một
```bash
cd project && python app_gradio.py     # UI Gradio, cổng 7860
cd project && python main.py           # REST API, cổng 8000
```

### Kiểm tra nhanh
```bash
curl http://127.0.0.1:8000/health      # {"ollama": true, "comfyui": true}
```
UI Gradio hiện `🟢 Ollama | 🟢 ComfyUI` ở đầu trang.

---

## Chạy test (không cần dịch vụ nào)
```bash
cd project && python test_router.py
```
Luôn chạy trước khi mở UI sau khi sửa code.

---

## Biến môi trường hay dùng

PowerShell (Windows):
```powershell
$env:OLLAMA_MODEL="qwen2.5:3b"          # model chat
$env:YUMEAI_CHAT_NUM_CTX="4096"         # context window; hạ 2048 nếu thiếu RAM
$env:YUMEAI_CHAT_HISTORY_MAX="12"       # số lượt giữ trong cửa sổ
$env:YUMEAI_CHAT_SUMMARY="1"            # trí nhớ dài; "0" để tắt
$env:YUMEAI_CHAT_FEWSHOT="1"            # few-shot giữ vai; "0" để tiết kiệm context
$env:YUMEAI_COMFY_TIMEOUT_SEC="1800"    # timeout một job ComfyUI
$env:YUMEAI_LOW_VRAM_MODE="1"           # "0" khi có GPU mạnh (bỏ cap steps)
$env:YUMEAI_FLUX_SAFE_STEPS="24"        # cap steps Flux
$env:YUMEAI_AUTO_MASK_EDIT="1"          # ưu tiên mask+inpaint thay Flux khi đổi đồ
$env:COMFY_MODELS_DIR="D:\...\models"   # chỉ cần khi để model ngoài project
```

Bash (Linux/macOS): thay `$env:X="y"` bằng `export X=y`.

---

## Sự cố thường gặp

### "🔴 Không kết nối được ComfyUI"
ComfyUI chưa chạy hoặc đã chết. Khởi động lại theo bước 2.

Cổng 8188 bị tiến trình cũ giữ:
```powershell
Get-NetTCPConnection -LocalPort 8188 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
```

### "ComfyUI đang chạy nhưng KHÔNG có checkpoint nào"
Thiếu file model. Tải một checkpoint SD 1.5 vào `ComfyUI/models/checkpoints/`
rồi bấm 🔄 Refresh trong UI.

### "⚠️ KHÔNG dịch được sang tiếng Anh"
Ollama chưa chạy. Prompt tiếng Việt đang vào thẳng Stable Diffusion → ảnh sẽ rác.
Bật `ollama serve` rồi thử lại.

### "⚠️ YumeAI đang xử lý job khác"
Resource Guard chặn 2 job ComfyUI chạy chồng (bảo vệ VRAM). Đợi job hiện tại xong.
Nếu chắc chắn không có job nào mà vẫn báo bận: đóng hết tiến trình Python và xoá
file khoá trong thư mục temp (`yumeai_comfyui_job.lock`).

### Nhân vật trả lời cụt, tuột vai, quên ngữ cảnh
Model chat quá nhỏ. Xem cảnh báo in ra lúc khởi động. Đổi sang `qwen2.5:3b` trở lên.

### Chat lâu rồi model trả lời lạc đề
Tràn context. Hạ `YUMEAI_CHAT_HISTORY_MAX` hoặc nâng `YUMEAI_CHAT_NUM_CTX` (nếu đủ RAM).

### Flux Kontext chạy mà ảnh không đổi
Theo thứ tự: tăng Guidance 3.5 → 4.0 → đổi model Kontext sang bản quant cao hơn
(Q4_K_M/Q5) bỏ vào `ComfyUI/models/unet`, app tự chọn bản tốt nhất → tăng Steps.

### Reset sạch
```powershell
taskkill /F /IM python.exe
```
Rồi khởi động lại từ bước 1.

---

## Chuyển dự án sang máy khác

Code đã sạch đường dẫn cứng. Nhưng phải mang theo/dựng lại:

| Thứ | Cách |
|---|---|
| `project/` (trừ `.venv`) | copy bình thường |
| `.venv/` | **KHÔNG copy** — tạo lại: `python -m venv .venv` + `pip install -r requirements.txt` |
| `ComfyUI/models/` | **~93GB**, không nằm trong git — copy tay |
| `ComfyUI/custom_nodes/` | ~1.4GB, 6 node — copy hoặc cài lại |
| Model Ollama | `ollama pull` lại trên máy mới |

Nếu để ComfyUI ở chỗ khác: đặt `COMFY_MODELS_DIR` và `COMFYUI_DIR`.
