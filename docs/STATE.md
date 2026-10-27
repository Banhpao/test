# Trạng thái dự án — YumeAI

**Cập nhật lần cuối:** 2026-10-26 · **Nhánh:** `review-fixes`

Trả lời đúng ba câu: đang chạy được gì, đang hỏng/thiếu gì, làm gì tiếp.
Cập nhật file này khi xong việc lớn hoặc phát hiện vấn đề lớn.

---

## ✅ Đang chạy được

**Mở app** (D23)

- Bấm đúp `MO-PAOCAKE.cmd`: bật Ollama trên ổ rời (`E:\Ollama`, cổng 11435), ComfyUI, `main.py`, rồi
  mở `/app`; đóng cửa sổ là tắt những gì nó đã bật. `project\.venv` và Ollama + model đều nằm trên
  ổ rời nên cắm máy Windows nào cũng chạy (ổ mang chữ E:).

**Trò chuyện**

- Chat streaming với persona Pao/Su (trước đây tên Yuki/Hana — D18), system prompt giàu chi tiết và few-shot.
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
- REST API lưu user message và assistant reply vào SQLite theo `conversation_id` —
  ghi cả hai khi đã có câu trả lời (từ 2026-10-15, cả Gradio), nên Ollama lỗi giữa chừng
  không còn để lại lượt user mồ côi. Mỗi lượt chỉ đọc cửa sổ lịch sử cần dùng.
- Đóng app/mở lại vẫn xem được lịch sử qua `http://127.0.0.1:8000/app`.
- Client cũ không có `conversation_id` vẫn dùng được trường `history` như trước.
- Fact extractor chạy nền mỗi 12 text message, tối đa 8 fact, cùng `num_ctx` với chat
  (JSON schema — Ollama < 0.5 trả 400 thì thử lại một lần với `format: "json"`; giữ
  phần mới nhất của lô, JSON hỏng 3 lần liền thì bỏ qua lô); fact được
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
- **Web UI `/app` làm lại (D19, 2026-10-22)** theo bản thiết kế "Ngân hà hồng": bốn màn
  Trò chuyện · Tạo ảnh · Bộ sưu tập · Chọn nhân vật, nền tối dịu mắt cho phòng tắt đèn,
  dùng được trên điện thoại. Chỉ gọi API đang có; chưa có màn sửa/tạo nhân vật, chưa có
  video, chọn khung hình hay chọn bộ vẽ (chờ lớp sinh ảnh mới của D17).
- Lần đầu mở `/app` có màn chào nói rõ Pao/Su là nhân vật AI và dữ liệu nằm ở `project/data`.
  Mục "Dữ liệu của bạn" (thanh bên) xuất zip toàn bộ dữ liệu và xoá toàn bộ (gõ "XOÁ");
  bộ sưu tập xoá được từng ảnh, đầu khung chat xoá được cuộc trò chuyện (D20).
- Nhắn "vẽ …"/"gửi ảnh đi" trong khung chat `/app` thì nhân vật tự vẽ và gửi ảnh vào hội
  thoại (`POST /api/chat/intent`, dùng chung bộ định tuyến với Gradio). Đường vẽ thật trên
  RTX 3050 vẫn **chưa chạy lần nào** — mới thử với ComfyUI giả.
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

**Kiểm chứng 2026-10-15** (Linux, venv tạm, Ollama/ComfyUI giả): `test_router.py`
pass (thêm nhóm 13–15: router, model, VRAM), `test_storage.py` 13/13,
`test_chat_context.py` 15/15, `test_memory.py` 11/11, `test_api_storage.py` 7/7 (test
cache tóm tắt đã sửa để đủ một lô D13). Chưa chạy trên máy thật.

**Kiểm chứng 2026-10-21** (Linux, ComfyUI/Ollama/nvidia-smi giả): `test_bench_baseline.py`
48/48 (cả Python không có venv); các test cũ cho kết quả như trước khi thêm bench. Bench
**chưa chạy trên máy thật** — đó là việc 0 bên dưới.

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

### Nợ kỹ thuật đã biết

| Vấn đề | Chi tiết |
|---|---|
| `app_gradio.py` là khối ~3800 dòng | Pipeline ảnh dính Gradio; REST mới dùng chung chat + text2img, chưa dùng được các edit pipeline nâng cao. |
| VRAM handoff (D16) chưa đo thật | Nhả Ollama trước job ComfyUI và `/free` trước vision mới kiểm bằng test giả; cần một lượt chạy trên RTX 3050 — `bench_baseline.py --warm-ollama` đo đúng bước này (việc 0). |
| Chưa có migration runner | Schema hiện ở v3, migrate bằng cách kiểm `PRAGMA table_info` rồi `ALTER TABLE` — idempotent và đã có test v1→v2→v3, nhưng vẫn chưa phải runner tuần tự thật. |
| Tóm tắt dùng cửa sổ trôi | Sau ~40 message overflow, fact từ đầu hội thoại rơi khỏi tóm tắt (D13). Đường bền vững là `memory_facts` — nay đã có ở **cả hai** cửa vào (D15). Làm đúng hẳn cần tóm tắt cuộn (summary cũ + phần mới → summary mới). |
| Ảnh từ Gradio chưa vào gallery | Bảng `images` mới chỉ được ghi ở đường REST. Cần hook ở `runtime.run_comfy_job` — điểm nghẽn duy nhất mà cả 8 chỗ gọi đều đi qua (D15). |
| Chưa test ảnh tự động | Test mới phủ storage/API; ComfyUI workflow vẫn cần service/model thật. |
| `ComfyUI/` không trong git | 93GB model + custom nodes phải copy/cài riêng khi chuyển máy. |

---

## 🎯 Việc tiếp theo, theo thứ tự

### 0. Đo nền giai đoạn 0 trên RTX 3050 (D17)

Chạy `project/bench_baseline.py` theo [`RUNBOOK.md`](RUNBOOK.md) mục "Đo nền (giai đoạn 0)"
— một lượt thường và một lượt `--warm-ollama` — rồi dán khối kết quả vào TESTLOG. Số này
là đầu vào cho hồ sơ máy của D17 (video ngắn từ ảnh persona, chỉ REST + `/app`) và đóng nợ
"VRAM handoff (D16) chưa đo thật". Chưa thêm model hay tính năng video nào trước khi có số.

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

Đã có (2026-10-22): gallery dạng lưới (D19), màn chào ghi rõ nhân vật AI, xoá ảnh, xoá hội
thoại, xuất và xoá toàn bộ dữ liệu (D20), sao lưu/khôi phục DB + ảnh bằng `sao_luu.py` (D21),
số đo hiệu năng thật mỗi lượt chat/ảnh + `GET /api/stats` (D22).
Còn: chưa có lần chạy thật nào trên máy này — dùng vài ngày rồi đọc `/api/stats`, chép số vào
TESTLOG.
Không cần age gate khi vẫn không có nội dung 18+ (PRODUCT_ROADMAP P3.1).
Chỉ thêm auth/rate-limit/CORS chặt khi có quyết định mở ngoài localhost.

### 5. Benchmark model chat

So sánh model 3B/4B local đang có trên đúng máy này bằng cùng bộ hội thoại persona,
đo RAM, token/giây, độ giữ vai và tiếng Việt; không chọn chỉ theo tên model.

Đã có (2026-10-22): `bench_chat.py` (RUNBOOK "So sánh model chat"). Đã chạy (2026-10-26, TESTLOG):
`qwen3:4b-instruct` giữ vai 23/24 và nhớ đúng 3/4, so với 17/24 và 0/4 của `qwen2.5:3b`, nhưng chậm
~4 lần trên GPU 4 GB (14.9 so với 64 token/giây). Đã chọn `qwen3:4b-instruct` làm mặc định (D24).
**Còn:** đo lại trên máy GPU mạnh hơn; theo dõi `reloads` ở `/api/stats` (đổi qua lại với model dịch
`qwen2.5:3b` trên GPU 4 GB); thử thêm ứng viên khác nếu cần.

Chi tiết: [`PRODUCT_ROADMAP.md`](PRODUCT_ROADMAP.md).

---

## 📌 Kỳ vọng cần giữ đúng

Model local nhỏ không cần trở thành trợ lý thông thái. Với companion, ưu tiên là
giữ vai, nhớ đúng, phản hồi tự nhiên và nhất quán hình ảnh. Mọi tính năng mới phải
được đánh giá theo bốn tiêu chí đó và giới hạn phần cứng Radeon 680M.


## Chuẩn bị Wan 2.2 5B trên ổ rời

Đã tải đủ 3 file Comfy-Org, đối chiếu SHA-256 và kiểm tra ComfyUI nhận model/node. Có workflow mẫu chính thức và bản thử 512x288, 17 khung hình trong ComfyUI/user/default/workflows. Chưa chạy sinh video hoặc tích hợp video vào YumeAI. Người dùng muốn giữ đủ bộ model và phát triển cấu hình chạy cho nhiều loại máy từ yếu đến mạnh ở bước sau. Chi tiết: E:/Python/WAN22_YUMEAI.md và E:/Python/manifests/wan22-5b.json.
