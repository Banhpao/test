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
> - [`docs/MAY.md`](docs/MAY.md) — máy đang dùng và giới hạn phần cứng (nơi duy nhất ghi điều này)

> **Dự án anh em:** `../yume_assistant` — **Yume Assistant**, trợ lý kỹ thuật cá
> nhân (RAG, tool calling, sau này ROS2/MQTT). Đó là **sản phẩm khác**, repo khác.
> Hai bên dùng chung vài ý tưởng kỹ thuật (personality system, context manager,
> Ollama client) nhưng personality mâu thuẫn trực tiếp — companion không bao giờ
> nhận mình là AI, assistant phải nói thẳng khi không chắc. **Đừng gộp hai dự án.**

---

## 1. Dự án này là gì

**YumeAI** — ứng dụng **AI companion chạy hoàn toàn local**, theo mô hình
[candy.ai](https://candy.ai): người dùng trò chuyện với một nhân vật cố định, và
nhân vật đó gửi ảnh của chính mình.

Ba trụ của sản phẩm, theo đúng thứ tự quan trọng:

1. **Nhân vật cố định** — cùng một Pao, ngày nào cũng vậy
2. **Trò chuyện có trí nhớ** — nhớ được người dùng là ai, đã nói gì
3. **Ảnh của chính nhân vật đó** — không phải một cô gái ngẫu nhiên

> ⚠️ **Sai lầm dễ mắc nhất khi mới vào dự án này:** nhìn thấy ~3800 dòng pipeline
> sửa ảnh trong `app_gradio.py` rồi tưởng đây là app chỉnh sửa ảnh. **Không phải.**
> Đó là di sản của giai đoạn đầu, và nó là *tính năng phụ*. Đừng đề xuất thêm tính
> năng sửa ảnh khi ba trụ trên còn chưa xong. Xem `docs/DECISIONS.md` mục D1.

Không phải: công cụ tạo ảnh dùng một lần, không phải dịch vụ image-to-video (xem D9).
Video ngắn từ ảnh của chính persona là tính năng phụ, chỉ bật khi máy đủ sức (xem D17).

---

## 2. Bản đồ code

```
Paocake_ai/
├── AGENTS.md, CLAUDE.md, docs/     ← bộ nhớ dự án (file này); máy đang dùng: docs/MAY.md
├── MO-PAOCAKE.cmd           bấm đúp để mở app: gọi project/khoi_dong.py (D23)
└── project/
    ├── app_gradio.py        ~3800 dòng — UI Gradio + TOÀN BỘ pipeline ảnh
    ├── main.py              ~850 dòng  — REST API + persistence cho web UI thật
    ├── chat-ui-mockup.html  UI web tại /app (D19): trò chuyện, tạo ảnh, bộ sưu tập, chọn nhân vật
    ├── test_router.py       ★ test chạy OFFLINE, không cần Ollama/ComfyUI
    ├── test_storage.py      test SQLite thuần, không cần dịch vụ
    ├── test_chat_context.py test trí nhớ dài: ĐẾM số lời gọi LLM tóm tắt
    ├── test_memory.py       test fact extraction theo lô, không gọi Ollama thật
    ├── test_api_storage.py  smoke test REST + SQLite với Ollama giả
    ├── test_rename_persona.py  test đổi tên Yuki → Pao, Hana → Su (D18): chuyển DB, sao lưu, id cũ
    ├── test_web_ui.py       test OFFLINE cho /app: chỉ gọi API có thật, giá trị khớp comfy_service
    ├── test_chat_image_route.py  test xin ảnh trong chat: /api/chat/intent + lưu câu xin ảnh
    ├── test_data_controls.py  test xoá ảnh/hội thoại, xuất zip, xoá toàn bộ dữ liệu (D20)
    ├── test_sao_luu.py      test OFFLINE cho sao lưu / khôi phục
    ├── test_perf.py         test OFFLINE cho số đo hiệu năng + /api/stats (D22)
    ├── devlog.py            ghi nhanh một dòng vào CHANGELOG
    ├── sao_luu.py           sao lưu / khôi phục DB + ảnh thành một file zip (D21)
    ├── bench_baseline.py    đo nền giai đoạn 0 (D17): GPU/RAM/đĩa + text2img cold/warm + D16
    ├── test_bench_baseline.py  test OFFLINE cho bench (ComfyUI/Ollama/nvidia-smi giả)
    ├── bench_chat.py        so sánh model chat: cùng bộ thoại Pao/Su, tốc độ + bộ nhớ + giữ vai
    ├── test_bench_chat.py   test OFFLINE cho bench_chat (Ollama giả bằng httpx.MockTransport)
    ├── khoi_dong.py         mở app một cú bấm: Ollama (ổ rời, cổng 11435) + ComfyUI + main.py (D23)
    ├── test_khoi_dong.py    test OFFLINE cho khoi_dong (chọn Ollama, cờ ComfyUI theo VRAM, tắt đúng cái mình bật)
    ├── test_auto_outfit.py  KHÔNG phải test tự động: công cụ thử tay đổi đồ, cần ComfyUI + Ollama + ảnh đầu vào
    ├── ComfyUI/             KHÔNG trong git — 93GB model, ~1.4GB custom_nodes
    └── yumeai/              ★ logic dùng chung — SỬA Ở ĐÂY, không copy sang nơi khác
        ├── core/
        │   ├── config.py          mọi biến môi trường, timeout, tham số chat
        │   ├── personas.py        hồ sơ nhân vật (tính cách, giọng nói, few-shot) + id cũ → mới
        │   ├── persona_prompt.py  dựng system prompt + few-shot từ hồ sơ
        │   ├── chat_context.py    lịch sử chat + trí nhớ dài + options Ollama
        │   ├── prompt_router.py   quyết định: chat / tạo ảnh / sửa ảnh
        │   ├── image_prompt.py    prompt text2img (positive + negative)
        │   ├── perf.py            số đo thật mỗi lượt chat/ảnh → data/perf.jsonl (chỉ số, D22)
        │   └── runtime.py         Resource Guard (chỉ 1 job ComfyUI cùng lúc)
        ├── services/
        │   ├── ollama_service.py  dịch, enrich, tóm tắt trí nhớ, chọn model
        │   ├── memory_service.py  fact extraction theo lô + lọc bí mật
        │   └── comfy_service.py   giao tiếp ComfyUI, preset chất lượng/upscale
        └── storage/
            └── database.py        SQLite: user/conversation/message/memory/image
```

**Quy tắc số một:** `app_gradio.py` và `main.py` là **hai cửa vào của cùng một hệ
thống**. Mọi logic dùng chung phải nằm trong `yumeai/`. Dự án đã một lần bị lệch
pha vì mỗi bên tự định nghĩa lại persona/prompt/preset — hậu quả là cùng một câu
lệnh cho ra hai kết quả khác nhau. Đừng lặp lại.

---

## 3. Chạy dự án

Nhanh nhất: bấm đúp **`MO-PAOCAKE.cmd`** — tự bật Ollama trên ổ rời, ComfyUI và `main.py`, rồi
mở `/app` (D23). Bên dưới là ba tiến trình nó bật, chạy tay được.
Chi tiết trong [`docs/RUNBOOK.md`](docs/RUNBOOK.md).

```bash
# 1) Ollama (chat, dịch, vision)        — BẮT BUỘC
#    MO-PAOCAKE.cmd tự bật bản trên ổ rời ở cổng 11435; chạy tay bản đó: RUNBOOK "Khởi động bằng tay".
#    Hoặc Ollama cài trên máy (cổng 11434, mặc định của OLLAMA_URL):
ollama serve

# 2) ComfyUI (mọi việc vẽ/sửa ảnh)      — BẮT BUỘC nếu muốn có ảnh
#    --lowvram khi GPU dưới 10 GB VRAM — máy hiện tại cần (docs/MAY.md)
cd project/ComfyUI && python main.py --lowvram

# 3) một trong hai cửa vào
cd project && python app_gradio.py      # UI Gradio, cổng 7860
cd project && python main.py            # REST API, cổng 8000
```

Giao diện web thật của REST API: `http://127.0.0.1:8000/app`.

**Không có ComfyUI thì không có ảnh nào.** ComfyUI không phải tuỳ chọn — nó *là*
bộ phận vẽ. `app_gradio.py`/`main.py` chỉ dựng JSON workflow rồi POST sang nó.

---

## 4. Trước khi sửa code — chạy test

Chạy **mọi** file `project/test_*.py` (trừ `test_auto_outfit.py` — công cụ thử tay, xem bản đồ code).
Không liệt kê tay ở đây: thêm file test mới là nó tự vào lệnh dưới.

```powershell
# Windows, ổ rời: chạy hết + khởi động thử ComfyUI, ghi nhật ký vào E:\duan\_vscode\nhat-ky\
E:\duan\_vscode\KIEM-THU-DU-AN.cmd

# Hoặc tay, trong project\ (PowerShell):
Get-ChildItem test_*.py | Where-Object Name -ne test_auto_outfit.py | ForEach-Object { .\.venv\Scripts\python.exe $_.Name }
```

Chạy **offline**, không cần Ollama hay ComfyUI (các test cần dịch vụ sẽ tự bỏ qua
nếu dịch vụ đang chạy, và tự kiểm tra chiều ngược lại nếu không). Đây là lưới an
toàn duy nhất của dự án — luôn chạy nó trước khi mở UI.

`KIEM-THU-DU-AN.cmd` lấy danh sách test từ `PAOCAKE_TESTS` trong `E:\duan\_vscode\moi_truong.py`;
`KIEM-TAI-LIEU.cmd` (cùng thư mục) cảnh báo khi có file test chưa nằm trong danh sách đó.

---

## 5. Ràng buộc phần cứng — ảnh hưởng đến mọi quyết định kỹ thuật

Máy đang dùng và giới hạn của nó ghi ở **một chỗ duy nhất: [`docs/MAY.md`](docs/MAY.md)**
(hiện là laptop RTX 3050 4 GB VRAM rời, Ollama trên ổ rời cổng 11435). Đọc file đó trước khi
quyết định gì về model, VRAM hay tốc độ; đừng chép thông số máy vào file này.

Điều không đổi theo máy:

- Chỉ **một job ComfyUI** cùng lúc (Resource Guard trong `runtime.py`).
- Ollama và ComfyUI dùng chung VRAM thì phải nhường nhau (D16) — đừng giả định cả hai cùng nằm
  trên GPU.
- **Không train LoRA** khi chưa có GPU ~8–12 GB VRAM thật. Đây là rào cản lớn nhất cho
  character consistency.
- Giới hạn trong code (lọc SDXL, `COMFY_TIMEOUT_SEC` = 1800s, `--lowvram`) được đặt cho máy yếu;
  chỉ nới khi có số đo trên máy mạnh hơn ghi trong TESTLOG.

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
| Định nghĩa lại prompt/preset/persona **hoặc khối trí nhớ** ở entrypoint | Hai cửa vào cho hai kết quả. **Tất cả nằm trong `yumeai/`.** Trí nhớ dùng `memory_service.build_conversation_memory()`, xem D15. |
| Thêm tính năng sửa ảnh mới | Xem mục 1. Ba trụ sản phẩm còn chưa xong. |
| Băm khoá cache trên dữ liệu mà hàm bên dưới sẽ cắt bỏ | Khoá đổi mỗi lượt, cache không bao giờ trúng, mà input LLM lại giống hệt nhau → trả tiền tính lại đúng kết quả cũ. **Khoá phải bằng đúng thứ được gửi đi.** Xem D13. |
| Cache kết quả rỗng khi dịch vụ lỗi | Ollama chết trả `""`; cache lại là khoá cứng "không có trí nhớ" tới hết phiên dù dịch vụ đã sống lại. **`if summary:` trước khi cache.** |
| Cắt danh sách bằng `[:N]` khi phần MỚI mới là phần cần | `summarize_conversation()` từng giữ 6000 ký tự đầu → trí nhớ dài đóng băng ở đoạn đầu hội thoại, im lặng. Xem D13. |
| Lọc theo user bằng INNER JOIN sang bảng có khoá ngoài NULL được | Ảnh `conversation_id IS NULL` biến mất khỏi gallery vĩnh viễn. **Cột sở hữu nằm thẳng trên bảng.** Xem D14. |

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
