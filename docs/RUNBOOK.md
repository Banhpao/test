# Runbook — YumeAI

Lệnh vận hành và xử lý sự cố. Thay cho file ghi chú `project/text` cũ.

---

## Mở bằng một cú bấm (D23)

Bấm đúp **`Paocake_ai\MO-PAOCAKE.cmd`**. Nó:

1. Bật **Ollama trên ổ rời** (`<ổ rời>\Ollama`, cổng **11435**) nếu chưa chạy. Model nằm luôn trên
   ổ, máy nào cũng như nhau, không phải `ollama pull` lại. Ổ chưa có Ollama thì chạy một lần
   `E:\duan\_vscode\CHUAN-BI-OLLAMA.cmd`.
2. Bật **ComfyUI** trong một cửa sổ thu nhỏ: GPU dưới 10 GB VRAM (hoặc không đọc được) →
   `--lowvram` như mục dưới; từ 12 GB → VRAM bình thường và `YUMEAI_LOW_VRAM_MODE=0`.
3. Chạy **`main.py`** ngay trong cửa sổ đó và tự mở `http://127.0.0.1:8000/app` khi sẵn sàng.

Đóng: **Ctrl+C** (hoặc đóng cửa sổ). Ollama/ComfyUI do chính nó bật được tắt theo; cái đang chạy
sẵn từ trước thì để nguyên. App đã chạy sẵn thì nó chỉ mở trình duyệt.

Tuỳ chọn, gõ sau tên file trong cửa sổ lệnh:

```powershell
MO-PAOCAKE.cmd --model qwen3:4b-instruct   # đổi model chat
MO-PAOCAKE.cmd --gradio                    # app_gradio.py (cổng 7860) thay cho main.py
MO-PAOCAKE.cmd --khong-comfy               # chỉ chat
MO-PAOCAKE.cmd --kiem-tra                  # chỉ in sẽ dùng gì, không bật gì
```

Ollama ở chỗ khác: đặt `OLLAMA_URL` trước khi bấm — file này dùng đúng địa chỉ đó, không bật gì.
Không có Ollama trên ổ rời thì nó dùng Ollama cài trên máy (cổng 11434) nếu đang chạy.

> Cổng 11435 chứ không phải 11434: PaoDich bật Ollama riêng của nó ở 11434 với thư mục model
> khác. Chung cổng thì app này nói chuyện với Ollama của PaoDich và báo thiếu model.

Cách cũ, ba cửa sổ riêng, vẫn dùng được như dưới.

## Khởi động bằng tay (3 tiến trình, 3 cửa sổ riêng)

### 1. Ollama — BẮT BUỘC (chat, dịch, vision)
```bash
ollama serve
```
Kiểm tra: `ollama list` phải thấy model chat, model dịch và model vision.

Model cần có:
| Vai trò | Model | Ghi chú |
|---|---|---|
| Chat | `qwen3:4b-instruct` (mặc định, D24) | `qwen2.5:3b` nhanh hơn ~4 lần trên GPU 4 GB nhưng giữ vai kém hơn; `llama3.2:1b` quá nhỏ, app sẽ cảnh báo |
| Dịch VI→EN, tóm tắt trí nhớ | `qwen2.5:3b` | tự dò, ưu tiên qwen2.5; ép bằng `YUMEAI_TRANSLATE_MODEL` |
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

Khi chạy `main.py`, mở **http://127.0.0.1:8000/app**. Giao diện này dùng SQLite
thật cho sidebar, lịch sử chat và gallery. `chat-ui-mockup.html` không còn dùng
mock response/Picsum nữa.

Giao diện `/app` (D19) có bốn màn, đổi bằng thanh bên hoặc địa chỉ:
`/app#tro-chuyen`, `/app#tao-anh`, `/app#bo-suu-tap`, `/app#nhan-vat`. Font Nunito và
Playfair Display tải từ Google Fonts; máy không có mạng thì trình duyệt dùng font hệ
thống, mọi chức năng vẫn chạy.

Xin ảnh ngay trong chat: nhắn kiểu "vẽ cho anh một tấm em bên cửa sổ" hoặc "gửi ảnh đi".
Cần ComfyUI đang chạy; ảnh dùng mức chất lượng đang chọn ở màn Tạo ảnh. Kiểm tra định tuyến:
```bash
curl -X POST http://127.0.0.1:8000/api/chat/intent -H "Content-Type: application/json" -d "{\"message\": \"vẽ em bên cửa sổ\", \"persona_id\": \"pao\"}"
# {"task":"text2img","style":"Anime",...}
```

### Kiểm tra nhanh
```bash
curl http://127.0.0.1:8000/health      # {"ollama": true, "comfyui": true}
```
UI Gradio hiện `🟢 Ollama | 🟢 ComfyUI` ở đầu trang.

Kiểm tra DB/API:
```bash
curl http://127.0.0.1:8000/api/conversations?user_id=user_001
curl http://127.0.0.1:8000/api/gallery?user_id=user_001
curl "http://127.0.0.1:8000/api/memory?user_id=user_001&persona_id=pao"
curl http://127.0.0.1:8000/api/stats   # số đo thật: token/giây, thời gian vẽ theo preset
```

---

## Chạy test (không cần dịch vụ nào)
```bash
cd project && python test_router.py
cd project && python test_storage.py
cd project && python test_chat_context.py
cd project && python test_memory.py
cd project && python test_api_storage.py
cd project && python test_bench_baseline.py
cd project && python test_rename_persona.py
cd project && python test_web_ui.py
cd project && python test_chat_image_route.py
cd project && python test_data_controls.py
cd project && python test_sao_luu.py
cd project && python test_perf.py
cd project && python test_bench_chat.py
```
Luôn chạy trước khi mở UI sau khi sửa code.

---

## Đo nền (giai đoạn 0)

Đo máy thật trước khi thêm model/tính năng nào của D17: GPU/VRAM, RAM, chỗ trống trên ổ
chứa `ComfyUI/models` (cần ≥ 30 GiB cho model giai đoạn sau), thời gian text2img SD1.5
cold/warm, VRAM đỉnh và giá nhả VRAM Ollama (D16). Bench **không** khởi động dịch vụ, không
đổi biến môi trường, không ghi DB hay gallery; prompt cố định (phong cảnh, không persona).

Mở 3 cửa sổ PowerShell, dùng đúng Python bạn vẫn chạy `main.py`:

```powershell
# Cửa sổ 1 — Ollama (không bắt buộc; thiếu thì D16 ghi "Ollama không chạy")
ollama serve

# Cửa sổ 2 — ComfyUI, khởi động lại NGAY TRƯỚC khi đo để job 1 là cold thật.
# Đợi dòng "To see the GUI go to: http://127.0.0.1:8188".
cd E:\duan\Paocake_ai\project\ComfyUI
python main.py --lowvram

# Cửa sổ 3 — đóng app_gradio.py/main.py nếu đang vẽ, rồi:
cd E:\duan\Paocake_ai\project
python bench_baseline.py --check-only    # 1) chỉ xem GPU/RAM/đĩa/dịch vụ, không vẽ
python bench_baseline.py                 # 2) 3 job: job 1 cold + 2 job warm
python bench_baseline.py --warm-ollama   # 3) nạp model Ollama trước mỗi job → đo D16 thật
```

- Mỗi lượt ghi `project\data\bench\pc0_baseline_<thời điểm>.json` (hoặc
  `<YUMEAI_DATA_DIR>\bench\` nếu đã đặt biến này; số đo đầy đủ, kể cả chuỗi VRAM theo thời
  gian) và `..._testlog.md`. Dán nội dung file `_testlog.md` vào
  `docs/TESTLOG.md` ngay dưới dòng `---` đầu tiên, rồi điền dòng Kết luận.
- Trước job 1 bench gửi ComfyUI `/free` (nhả model + xoá cache trong bộ nhớ, không đụng
  file) để job 1 chắc chắn nạp lại checkpoint; `--no-free` để bỏ bước này.
- Trong lúc đo bench giữ Resource Guard: app gửi job ảnh lúc đó sẽ bị báo bận — đúng thiết kế.
- Ảnh thử nằm ở `ComfyUI\output\yumeai_bench\`, không vào gallery. Bench không xoá gì; muốn
  dọn thì xoá tay.
- Mã thoát: `0` xong · `1` có job lỗi (phần đã đo vẫn được ghi) · `2` không bắt đầu được
  (ComfyUI tắt/đang bận/thiếu checkpoint) · `130` dừng bằng Ctrl+C.
- Tuỳ chọn khác: `--runs N` (1–20), `--seed S`, `--timeout GIÂY`, `--out-dir THƯ_MỤC`;
  xem `python bench_baseline.py --help`.

---

## So sánh model chat

Chạy cùng một bộ thoại cố định (Pao và Su, 6 câu mỗi người) qua từng model Ollama đã pull,
đi đúng đường prompt của app. Đo nạp cold, bộ nhớ và phần nằm trên GPU, token/giây, chờ chữ
đầu; chấm giữ vai bằng luật: tiếng Việt, không lẫn chữ Hán, không emoji, không gạch đầu dòng,
không tự nhận là AI, xưng hô đúng (Pao tao/mày ↔ tớ/cậu, Su em/anh), không dài dòng hay bị cắt,
nhớ đúng thông tin trong trí nhớ.

Nhanh nhất: bấm đúp `E:\duan\_vscode\SO-SANH-MODEL-CHAT.cmd` — bật Ollama trên ổ rời, so
`qwen2.5:3b` với `qwen3:4b-instruct`, xong thì tắt. Chạy tay:

```powershell
# Đóng ComfyUI hoặc để nó rảnh (đang vẽ thì bench từ chối). main.py để chạy cũng được.
# Ollama trên ổ rời (MO-PAOCAKE.cmd đang mở thì nó đã bật sẵn):
$env:OLLAMA_URL="http://127.0.0.1:11435"
cd E:\duan\Paocake_ai\project
python bench_chat.py --check-only                       # 1) model nào đã pull, VRAM, ComfyUI
python bench_chat.py                                    # 2) chỉ model đang dùng (OLLAMA_MODEL)
python bench_chat.py --models qwen2.5:3b hermes3:3b     # 3) so nhiều model, lần lượt
```

- Bench **không tải model**. Model chưa có thì bỏ qua và in lệnh `ollama pull <tên>` để bạn tự
  chạy (mỗi model 3–4B khoảng 2–3 GB). Ví dụ ứng viên cho máy 4 GB VRAM: `qwen3:4b-instruct`
  (đang dùng, D24), `qwen2.5:3b`, `hermes3:3b`, `llama3.2:3b`, `gemma3:4b`.
- Mỗi model: nhả mọi model Ollama đang nạp → nạp model đó (đo cold) → chạy bộ thoại `--rounds`
  vòng (mặc định 2, mỗi vòng một seed) → nhả ra. App tự nạp lại model của nó ở câu chat sau.
  `--no-free` để không nhả gì (khi đó số nạp cold không đúng).
- Kết quả: `project\data\bench\chat_<thời điểm>.json` (kèm **mọi câu trả lời** — đọc chúng,
  luật chỉ bắt lỗi thô) và `..._testlog.md`; dán khối đó vào `docs/TESTLOG.md` dưới dòng
  `---` đầu tiên rồi điền Kết luận.
- Bộ thoại, trí nhớ giả (Minh, lập trình viên, trà sữa ít đường) và seed cố định để hai lần đo
  so được với nhau. Không đụng DB, gallery hay ComfyUI.
- Mã thoát: `0` xong · `1` có câu lỗi (phần đã đo vẫn được ghi) · `2` không bắt đầu được (Ollama
  tắt, không có model nào, ComfyUI đang vẽ) · `130` Ctrl+C (vẫn ghi phần đã đo).
- Tuỳ chọn khác: `--personas pao`, `--seed S`, `--timeout GIÂY`, `--out-dir`; xem
  `python bench_chat.py --help`.

---

## Số đo khi dùng thật (D22)

Bench ở trên đo một lần. Còn lúc dùng `/app`, `main.py` tự ghi mỗi lượt chat và mỗi ảnh vào
`project\data\perf.jsonl` (một dòng JSON mỗi lần), rồi tóm lại ở:

```powershell
curl http://127.0.0.1:8000/api/stats
```

- `chat.models[]` — theo từng `OLLAMA_MODEL` đã dùng, model mới nhất đứng đầu: `tokens_per_s`
  (tốc độ sinh chữ), `first_token_s` (chờ bao lâu mới thấy chữ đầu, gồm cả dựng trí nhớ),
  `total_s`, `prompt_tokens` (prompt dài ra theo lịch sử), `reloads` (số lượt Ollama phải nạp
  lại model ≥ 1 giây — thường do vừa nhường VRAM cho ComfyUI), `truncated` (câu bị cắt vì chạm
  `YUMEAI_CHAT_NUM_PREDICT`), `failed`, `cancelled` (đóng tab giữa chừng).
- `image.presets[]` — theo cặp chất lượng + phóng to: `total_s` (cả lượt), `comfy_s` (ComfyUI
  vẽ, gồm nhường VRAM), `translate_s` (dịch/viết prompt), `failed`, `busy`, `models`.
- Mỗi mục là `{n, median, min, max}` trên 500 lần đo gần nhất; chỉ lần thành công mới tính thời
  gian. Màn Tạo ảnh của `/app` dùng chính số này để ghi "thường mất khoảng…".
- So model chat: đổi `OLLAMA_MODEL`, khởi động lại `main.py`, chat như thường vài chục lượt —
  `/api/stats` tách số theo từng model.
- Sổ chỉ có số, tên model và preset — không có tin nhắn, prompt hay tên người dùng. Tự xoay vòng
  ở ~2 MB (`perf.jsonl.1` là bản cũ). Tắt: `$env:YUMEAI_PERF_LOG="0"`. Xoá tay bất cứ lúc nào
  cũng được; **Xoá toàn bộ dữ liệu** trong `/app` cũng xoá nó.

---

## Biến môi trường hay dùng

PowerShell (Windows):
```powershell
$env:OLLAMA_MODEL="qwen3:4b-instruct"   # model chat (mặc định, D24); "qwen2.5:3b" nếu cần nhanh
$env:YUMEAI_CHAT_NUM_CTX="4096"         # context window; hạ 2048 nếu thiếu RAM
$env:YUMEAI_CHAT_HISTORY_MAX="12"       # số lượt giữ trong cửa sổ
$env:YUMEAI_CHAT_SUMMARY="1"            # trí nhớ dài; "0" để tắt
$env:YUMEAI_CHAT_FEWSHOT="1"            # few-shot giữ vai; "0" để tiết kiệm context
$env:YUMEAI_COMFY_TIMEOUT_SEC="1800"    # timeout một job ComfyUI
$env:YUMEAI_LOW_VRAM_MODE="1"           # "0" khi có GPU mạnh (bỏ cap steps)
$env:YUMEAI_FLUX_SAFE_STEPS="24"        # cap steps Flux
$env:YUMEAI_AUTO_MASK_EDIT="1"          # ưu tiên mask+inpaint thay Flux khi đổi đồ
$env:COMFY_MODELS_DIR="D:\...\models"   # chỉ cần khi để model ngoài project
$env:YUMEAI_DATA_DIR="D:\YumeAIData"   # DB + ảnh; mặc định project/data
$env:YUMEAI_MEMORY_FACTS="1"             # "0" để tắt fact extraction nền
$env:YUMEAI_MEMORY_EVERY="12"            # số text message mỗi batch
$env:YUMEAI_MEMORY_BATCH_SIZE="24"       # tối đa message đọc mỗi lần
$env:YUMEAI_VRAM_HANDOFF="1"             # nhường VRAM Ollama ↔ ComfyUI (D16); "0" để tắt
$env:YUMEAI_PERF_LOG="1"                 # ghi số đo vào data/perf.jsonl (D22); "0" để tắt
$env:YUMEAI_OLLAMA_NUM_GPU="0"           # tuỳ chọn: ép Ollama chạy CPU, nhường VRAM cho ComfyUI
$env:YUMEAI_VISION_MODEL="qwen2.5vl:3b"  # tuỳ chọn: ép model vision thay vì tự dò theo tên
$env:YUMEAI_TRANSLATE_MODEL="qwen2.5:3b" # tuỳ chọn: ép model dịch/tóm tắt
```

Bash (Linux/macOS): thay `$env:X="y"` bằng `export X=y`.

---

## Dữ liệu và backup

Mặc định dữ liệu sản phẩm nằm tại:

```text
project/data/yumeai.db   # user, persona, conversation, message, memory, metadata ảnh
project/data/images/     # file PNG đã tạo
project/data/perf.jsonl  # số đo tốc độ chat/vẽ (D22) — chỉ số, không nội dung
```

`project/data/` đã được gitignore. Để backup khi app đã dừng, copy cả thư mục
`data/`; không chỉ copy file DB vì gallery tham chiếu các PNG trong `images/`.
Có thể đặt `YUMEAI_DATA_DIR` sang ổ ngoài trước khi khởi động.

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
Model chat quá nhỏ. Xem cảnh báo in ra lúc khởi động. Dùng `qwen3:4b-instruct` (mặc định) hoặc
model 3B trở lên.

### Chat lâu rồi model trả lời lạc đề
Tràn context. Hạ `YUMEAI_CHAT_HISTORY_MAX` hoặc nâng `YUMEAI_CHAT_NUM_CTX` (nếu đủ RAM).

### Flux Kontext chạy mà ảnh không đổi
Theo thứ tự: tăng Guidance 3.5 → 4.0 → đổi model Kontext sang bản quant cao hơn
(Q4_K_M/Q5) bỏ vào `ComfyUI/models/unet`, app tự chọn bản tốt nhất → tăng Steps.

### Dữ liệu của bạn: xuất, xoá
Trong `/app`, bấm tên mình ở cuối thanh bên → **Dữ liệu của bạn**:
- **Xuất toàn bộ dữ liệu** tải về `yumeai-du-lieu-<ngày>.zip` (gồm `data.json` + thư mục `images/`).
  Cùng file đó lấy được bằng `curl -o du-lieu.zip "http://127.0.0.1:8000/api/data/export?user_id=user_001"`.
- **Xoá toàn bộ** cần gõ `XOÁ`. Xoá thật, không tự sao lưu (D20), xoá luôn sổ đo
  `perf.jsonl` (D22). Các file trong
  `project/data/backups/` (bản sao lưu lúc đổi tên nhân vật…) không bị đụng tới — muốn sạch
  hẳn thì xoá tay thư mục đó.
- Xoá một ảnh: Bộ sưu tập → chọn ảnh → **Xoá ảnh này** (ảnh mất cả khỏi hội thoại chứa nó).
  Xoá một cuộc trò chuyện: nút thùng rác ở đầu khung chat (ảnh vẫn ở lại bộ sưu tập, ký ức
  của nhân vật về bạn vẫn giữ).

### Sao lưu và khôi phục (DB + ảnh)
```bash
cd project && python sao_luu.py                 # tạo data/backups/sao-luu-<thời điểm>.zip
cd project && python sao_luu.py --danh-sach     # xem các bản đang có
cd project && python sao_luu.py --khoi-phuc data/backups/sao-luu-20261022-080000.zip
```
- Sao lưu chạy được cả khi `main.py` đang mở (chụp DB bằng backup API của sqlite).
- Khôi phục **từ chối** khi `main.py` (8000) hoặc `app_gradio.py` (7860) đang chạy — tắt hết
  trước. Nó kiểm bản sao lưu (integrity_check, schema không mới hơn code), hỏi lại, rồi
  **luôn** lưu dữ liệu hiện tại thành `truoc-khoi-phuc-<thời điểm>.zip` trước khi thay.
- Chỉ dùng thư viện chuẩn: không cần venv. Chuyển máy: copy file zip sang, cài dự án, chạy
  `--khoi-phuc`.

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
| `.venv/` | Trên ổ rời E: nó dựng từ Python portable (`E:\Python\runtimes`) nên cắm máy Windows nào cũng chạy, miễn ổ mang chữ E:. Chép sang ổ/chỗ khác thì **không** copy — tạo lại bằng `E:\duan\MAY-MOI.cmd` hoặc `python -m venv .venv` + cài lại thư viện |
| `ComfyUI/models/` | **~93GB**, không nằm trong git — copy tay |
| `ComfyUI/custom_nodes/` | ~1.4GB, 6 node — copy hoặc cài lại |
| Ollama + model | Trên ổ rời: `<ổ>\Ollama` (chương trình + model), không phải tải lại. Ổ khác: chạy `_vscode\CHUAN-BI-OLLAMA.cmd`, hoặc cài Ollama rồi `ollama pull` |

Nếu để ComfyUI ở chỗ khác: đặt `COMFY_MODELS_DIR` và `COMFYUI_DIR`.
