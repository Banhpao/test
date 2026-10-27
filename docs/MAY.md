# Máy đang dùng — YumeAI

**Cập nhật:** 2026-10-27

Nơi **duy nhất** ghi dự án đang chạy trên máy nào và điều đó buộc code phải làm gì. AGENTS.md,
STATE.md, TESTLOG.md, RUNBOOK.md dẫn link về đây, không chép lại. Đổi máy thì sửa file này trước,
rồi ghi kết quả chạy thử vào [`TESTLOG.md`](TESTLOG.md).

---

## Máy hiện tại (đã kiểm chứng trên máy thật, 2026-10-26)

| | |
|---|---|
| Máy | Laptop `LAPTOP-1AMPNHOP` |
| GPU | **NVIDIA GeForce RTX 3050 Laptop GPU, 4 GB VRAM rời** |
| RAM | 31,7 GiB |
| HĐH | Windows (Python báo `Windows-10-10.0.26200`) |
| Dự án | Trên ổ rời, luôn mang chữ **E:** (`E:\duan\Paocake_ai`) |
| Python | `project\.venv` dựng từ Python portable 3.11.16 trên ổ rời (`E:\Python\runtimes`), gồm cả thư viện ComfyUI (torch 2.12.1+cu130) |
| Ollama | Bản zip trên ổ rời `E:\Ollama`, cổng **11435**, model nằm trên ổ: `qwen3:4b-instruct`, `qwen2.5:3b` |

Nguồn số: [`TESTLOG.md`](TESTLOG.md) mục 2026-10-26 và nhật ký `E:\duan\_vscode\nhat-ky\` ngày 26/10.

## Hệ quả cho code — đúng với máy này

- **Mở app:** `MO-PAOCAKE.cmd` (D23). Nó tự bật Ollama trên ổ rời ở cổng 11435 và chọn cờ ComfyUI
  theo VRAM: dưới 10 GB → `--lowvram`; từ 12 GB → VRAM bình thường + `YUMEAI_LOW_VRAM_MODE=0`.
  Máy 4 GB này luôn chạy `--lowvram`.
- **Model chat mặc định `qwen3:4b-instruct`** (D24): 3,3 GiB, chỉ 67% nằm trên GPU, 14,9 token/giây.
  `qwen2.5:3b` nằm trọn GPU, nhanh ~4 lần nhưng giữ vai kém hơn. Dịch VI→EN và tóm tắt trí nhớ
  vẫn tự dò, ưu tiên `qwen2.5:3b` — hai model có thể phải đổi chỗ nhau trên GPU (`reloads` ở
  `/api/stats`, D22).
- **Ollama và ComfyUI nhường VRAM cho nhau** (D16): 4 GB không chứa được cả hai. Chưa đo thật —
  việc 0 trong STATE.
- **Chỉ một job ComfyUI cùng lúc** (Resource Guard, `runtime.py`).
- **Không SDXL, không train LoRA** trên máy này: `get_checkpoint_model()` vẫn lọc SDXL; train LoRA
  cần ~8–12 GB VRAM thật.
- **Chưa đo trên máy này:** vẽ ảnh thật (SD 1.5, Flux Kontext), tốc độ đường REST `/app`, video Wan 2.2.
  Số Flux Kontext ~400 giây/ảnh trong TESTLOG là của máy Radeon cũ, không áp cho máy này.
- **Model vision cho pipeline đổi đồ** (`qwen2.5vl`, RUNBOOK) **chưa có** trong Ollama trên ổ rời.

## Máy trước đây

Trước 11/9/2026 (TESTLOG ghi RTX 3050 từ ngày đó): AMD Radeon 680M (iGPU), "VRAM" 4 GB cắt từ 16 GB RAM, hệ điều hành thấy
~12,4 GB; Ollama chạy RAM/CPU. Các quyết định D3–D15 trong [`DECISIONS.md`](DECISIONS.md) được viết
cho máy đó — giữ nguyên như lịch sử, xem D16/D17 cho phần đã đổi. Số đo cũ: TESTLOG mục
"Tham chiếu máy phát triển trước đây".

## Khi đổi sang máy khác

1. Cắm ổ (chữ E:), bấm `E:\duan\MAY-MOI.cmd` — kiểm tra và dựng lại môi trường nếu cần.
2. Bấm `E:\duan\_vscode\KIEM-THU-DU-AN.cmd` — chạy toàn bộ test offline.
3. Chạy `bench_chat.py` và `bench_baseline.py` (RUNBOOK), dán kết quả vào TESTLOG.
4. Sửa bảng "Máy hiện tại" ở trên; chuyển máy cũ xuống "Máy trước đây".
