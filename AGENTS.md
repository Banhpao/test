# YumeAI — hướng dẫn cho AI làm việc trên dự án này

> **Đọc file này trước khi làm bất cứ việc gì.** Nó tồn tại để bạn không phải hỏi
> lại chủ dự án những thứ đã được quyết định rồi.
>
> Đọc thêm khi cần:
> - [`docs/STATE.md`](docs/STATE.md) — đang chạy được gì, đang hỏng gì, làm gì tiếp
> - [`docs/DECISIONS.md`](docs/DECISIONS.md) — **vì sao** mọi thứ như hiện tại, và hướng nào đã bị loại
> - [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — đã đổi gì, ngày nào
> - [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — lệnh khởi động, xử lý sự cố
> - [`docs/TESTLOG.md`](docs/TESTLOG.md) — kết quả chạy thử trên các máy khác nhau

---

## 1. Dự án này là gì

**YumeAI** — ứng dụng **AI companion chạy hoàn toàn local**, theo mô hình
[candy.ai](https://candy.ai): người dùng trò chuyện với một nhân vật cố định, và
nhân vật đó gửi ảnh của chính mình.

Ba trụ của sản phẩm, theo đúng thứ tự quan trọng:

1. **Nhân vật cố định** — cùng một Yuki, ngày nào cũng vậy
2. **Trò chuyện có trí nhớ** — nhớ được người dùng là ai, đã nói gì
3. **Ảnh của chính nhân vật đó** — không phải một cô gái ngẫu nhiên

> ⚠️ **Sai lầm dễ mắc nhất khi mới vào dự án này:** nhìn thấy ~3800 dòng pipeline
> sửa ảnh trong `app_gradio.py` rồi tưởng đây là app chỉnh sửa ảnh. **Không phải.**
> Đó là di sản của giai đoạn đầu, và nó là *tính năng phụ*. Đừng đề xuất thêm tính
> năng sửa ảnh khi ba trụ trên còn chưa xong. Xem `docs/DECISIONS.md` mục D1.

Không phải: công cụ tạo ảnh dùng một lần, không phải image-to-video (xem D9).

---

## 2. Bản đồ code

```
formyhorny/
├── AGENTS.md, CLAUDE.md, docs/     ← bộ nhớ dự án (file này)
└── project/
    ├── app_gradio.py        ~3800 dòng — UI Gradio + TOÀN BỘ pipeline ảnh
    ├── main.py              ~200 dòng  — REST API (FastAPI) cho web UI thật
    ├── chat-ui-mockup.html  UI web mẫu, gọi localhost:8000
    ├── test_router.py       ★ test chạy OFFLINE, không cần Ollama/ComfyUI
    ├── devlog.py            ghi nhanh một dòng vào CHANGELOG
    ├── ComfyUI/             KHÔNG trong git — 93GB model, ~1.4GB custom_nodes
    └── yumeai/              ★ logic dùng chung — SỬA Ở ĐÂY, không copy sang nơi khác
        ├── core/
        │   ├── config.py          mọi biến môi trường, timeout, tham số chat
        │   ├── personas.py        hồ sơ nhân vật (tính cách, giọng nói, few-shot)
        │   ├── persona_prompt.py  dựng system prompt + few-shot từ hồ sơ
        │   ├── chat_context.py    lịch sử chat + trí nhớ dài + options Ollama
        │   ├── prompt_router.py   quyết định: chat / tạo ảnh / sửa ảnh
        │   ├── image_prompt.py    prompt text2img (positive + negative)
        │   └── runtime.py         Resource Guard (chỉ 1 job ComfyUI cùng lúc)
        └── services/
            ├── ollama_service.py  dịch, enrich, tóm tắt trí nhớ, chọn model
            └── comfy_service.py   giao tiếp ComfyUI, preset chất lượng/upscale
```

**Quy tắc số một:** `app_gradio.py` và `main.py` là **hai cửa vào của cùng một hệ
thống**. Mọi logic dùng chung phải nằm trong `yumeai/`. Dự án đã một lần bị lệch
pha vì mỗi bên tự định nghĩa lại persona/prompt/preset — hậu quả là cùng một câu
lệnh cho ra hai kết quả khác nhau. Đừng lặp lại.

---

## 3. Chạy dự án

Cần **ba tiến trình riêng biệt**. Chi tiết trong [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```bash
# 1) Ollama (chat, dịch, vision)        — BẮT BUỘC
ollama serve

# 2) ComfyUI (mọi việc vẽ/sửa ảnh)      — BẮT BUỘC nếu muốn có ảnh
cd project/ComfyUI && python main.py --lowvram

# 3) một trong hai cửa vào
cd project && python app_gradio.py      # UI Gradio, cổng 7860
cd project && python main.py            # REST API, cổng 8000
```

**Không có ComfyUI thì không có ảnh nào.** ComfyUI không phải tuỳ chọn — nó *là*
bộ phận vẽ. `app_gradio.py`/`main.py` chỉ dựng JSON workflow rồi POST sang nó.

---

## 4. Trước khi sửa code — chạy test

```bash
cd project && python test_router.py
```

Chạy **offline**, không cần Ollama hay ComfyUI (các test cần dịch vụ sẽ tự bỏ qua
nếu dịch vụ đang chạy, và tự kiểm tra chiều ngược lại nếu không). Đây là lưới an
toàn duy nhất của dự án — luôn chạy nó trước khi mở UI.

---

## 5. Ràng buộc phần cứng — ảnh hưởng đến mọi quyết định kỹ thuật

| | |
|---|---|
| GPU | **AMD Radeon 680M — iGPU tích hợp**, không có VRAM riêng |
| "VRAM" | 4GB cắt từ RAM hệ thống (UMA Frame Buffer, chỉnh trong BIOS) |
| RAM | 16GB thực, hệ điều hành thấy ~12.4GB (4GB kia đã thành VRAM) |

Hệ quả bắt buộc phải nhớ:

- **Không dùng SDXL.** `get_checkpoint_model()` lọc bỏ SDXL có chủ đích.
- Flux Kontext chạy GGUF **Q3**, mất **~400 giây/ảnh**. `COMFY_TIMEOUT_SEC` = 1800s.
- ComfyUI phải chạy `--lowvram`.
- Chỉ **một job ComfyUI** cùng lúc (Resource Guard trong `runtime.py`).
- Ollama chạy trên **RAM/CPU**, không đụng VRAM. Model chat nên ≤ 7B.
- **Nâng RAM không làm ComfyUI nhanh hơn** — iGPU mới là nút thắt. Cần GPU rời.
- **Không train LoRA được trên máy này** (cần ~8–12GB VRAM thật). Đây là rào cản
  lớn nhất cho character consistency.

---

## 6. Quy ước code

- **Comment và thông báo lỗi viết bằng tiếng Việt.** Toàn bộ dự án như vậy.
- Comment nên nói **vì sao**, đặc biệt khi sửa bug: ghi lại hành vi sai của bản cũ
  để người sau không "sửa ngược" lại.
- **Giữ nguyên kiểu xuống dòng.** `main.py` và `yumeai/core/prompt_router.py` dùng
  **CRLF**; các file khác dùng LF. Đổi nhầm là git báo thay đổi cả file.
- Giữ dự án **pyflakes sạch**: `python -m pyflakes app_gradio.py main.py yumeai/`
- `app_gradio.py` có một `assert` lúc khởi động kiểm tra số phần tử `shared_inputs`
  khớp số tham số của `smart_unified_chat`. Thêm input UI thì phải thêm tham số,
  **và đúng thứ tự**.

---

## 7. Những điều TUYỆT ĐỐI đừng làm

Mỗi dòng dưới đây là một bug đã thực sự xảy ra và đã tốn thời gian để tìm ra.

| Đừng | Vì sao |
|---|---|
| Dùng `"từ khoá" in text` để dò tiếng Việt | `"hi" in "khi nào"` → True. `"đồ" in "đồng ý"` → True. Router nhảy loạn. **Dùng `contains_word()` trong `prompt_router.py`.** |
| Để router mặc định là `text2img` khi không match rule | Chat bình thường bị đem đi vẽ tranh. **Mặc định phải là `chat`.** |
| Lọc lịch sử chat bằng `content.startswith(emoji)` | Message người dùng có ảnh bắt đầu bằng `📎` → mất sạch khỏi ngữ cảnh. **Dùng `strip_ui_markers()`.** |
| Lấy khoá ở hàm này, nhả ở hàm khác | Resource Guard từng kẹt vĩnh viễn. **Dùng `run_comfy_job()` / `comfy_job()`.** |
| Chốt tên model lúc import | Ollama chưa bật là khoá cứng cả phiên. **Dùng `get_translate_model()`.** |
| Trả về chuỗi rỗng khi lỗi rồi để bên gọi tự đoán | "Không tìm thấy model!" trong khi thủ phạm là ComfyUI chưa bật. **Ném lỗi có hướng dẫn cụ thể.** |
| Nuốt lỗi dịch rồi vẽ tiếp | Prompt tiếng Việt vào thẳng SD → ảnh rác mà không ai biết vì sao. **Dùng `translate_vi_en_ex()` và hiện cảnh báo.** |
| Định nghĩa lại prompt/preset/persona ở entrypoint | Hai cửa vào cho hai kết quả. **Tất cả nằm trong `yumeai/`.** |
| Thêm tính năng sửa ảnh mới | Xem mục 1. Ba trụ sản phẩm còn chưa xong. |

---

## 8. Khi bạn thay đổi gì đó

Ghi lại, để người (hoặc AI) sau không phải dò từ đầu:

```bash
cd project && python devlog.py "Mô tả ngắn thay đổi"
```

Nếu là **quyết định thiết kế** (chọn hướng, loại bỏ phương án, đánh đổi) thì thêm
một mục vào [`docs/DECISIONS.md`](docs/DECISIONS.md) — quan trọng hơn changelog
nhiều, vì nó ngăn người sau đi lại con đường đã bị loại.

Nếu trạng thái dự án đổi (xong việc lớn, phát hiện bug lớn) thì cập nhật
[`docs/STATE.md`](docs/STATE.md).

Nếu bạn **chạy thử trên máy khác** (GPU rời, cấu hình khác) thì ghi kết quả vào
[`docs/TESTLOG.md`](docs/TESTLOG.md) theo mẫu có sẵn — rất nhiều giới hạn trong code
là do máy phát triển yếu, file đó cho biết giới hạn nào là thật.
