# YumeAI

👉 **Toàn bộ ngữ cảnh dự án nằm ở [`AGENTS.md`](AGENTS.md) — đọc file đó trước.**

Tóm tắt một dòng: ứng dụng **AI companion chạy local** (mô hình candy.ai) —
ComfyUI vẽ ảnh, Ollama lo ngôn ngữ. Sản phẩm là *nhân vật cố định + trò chuyện có
trí nhớ + ảnh của chính nhân vật đó*, **không phải** công cụ chỉnh sửa ảnh.

| File | Nội dung |
|---|---|
| [`AGENTS.md`](AGENTS.md) | Bản đồ code, cách chạy, quy ước, **những điều tuyệt đối đừng làm** |
| [`docs/STATE.md`](docs/STATE.md) | Đang chạy được gì, hỏng gì, làm gì tiếp |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Vì sao mọi thứ như hiện tại, hướng nào đã bị loại |
| [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | Đã đổi gì, ngày nào |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Lệnh khởi động, xử lý sự cố |
| [`docs/TESTLOG.md`](docs/TESTLOG.md) | Kết quả chạy thử trên các máy khác nhau |

Trước khi sửa code: `cd project && python test_router.py`
Sau khi sửa code: `cd project && python devlog.py "mô tả thay đổi"`
