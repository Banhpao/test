# Nhật ký thay đổi — YumeAI

Mục mới nhất ở **trên cùng**. Ghi nhanh một dòng bằng:

```bash
cd project && python devlog.py "Mô tả ngắn thay đổi"
```

Quyết định thiết kế thì ghi vào [`DECISIONS.md`](DECISIONS.md), không ghi ở đây.

---

## 2026-10-28

- Chạy lại kịch bản "Tuấn" của 27/10 trên dữ liệu riêng (`_vscode\MO-PAOCAKE-DU-LIEU-THU.cmd` mở app với `YUMEAI_DATA_DIR` = `project\data\thu-lai\<ngày-giờ>`, không đụng DB thật): kết quả ở TESTLOG 28/10. Trích fact 8/8 và 6/6 đúng (27/10: 6/13, 5/7); lỗi 2, 3, 4 hết; ảnh có câu kèm, không còn `1pao`.
- Sửa thêm sau lần chạy lại (D28): tóm tắt hội thoại chỉ giữ dòng dựng từ lời người dùng (`ground_summary()`, áp cả bản đã lưu); `/api/chat` lọc emoji trên luồng token (`strip_emoji()`); ảnh nhân vật bỏ tag tóc/mắt/mặt model tự thêm (`drop_look_tags()`). Test: `test_chat_context.py` 18 (bộ tóm tắt giả nay trả câu người dùng và được đặt lại mỗi test), `test_memory.py` 22, `test_router.py` thêm 3 kiểm.
- Commit `7446196` (6 lỗi 27/10) đã đẩy lên `review-fixes`; 14 test Paocake + ComfyUI đạt trên máy trước khi commit.

---

## 2026-10-27

- Tài liệu (không đổi mã): thêm `docs/MAY.md` — nơi duy nhất ghi máy đang dùng (laptop RTX 3050 4 GB, Ollama trên ổ rời cổng 11435) và hệ quả cho code; AGENTS mục 5, STATE, TESTLOG, PRODUCT_ROADMAP dẫn link về đó thay vì chép thông số. Bỏ chỗ gọi Radeon 680M là "máy hiện tại"; sơ đồ code `formyhorny/` → `Paocake_ai/`.
- AGENTS mục 4: chạy mọi `project/test_*.py` (hoặc `E:\duan\_vscode\KIEM-THU-DU-AN.cmd`) thay vì liệt kê tay sáu file; ghi `test_auto_outfit.py` là công cụ thử tay. RUNBOOK: chạy tay Ollama trên ổ rời (`OLLAMA_HOST`/`OLLAMA_MODELS`/`OLLAMA_URL`), ghi `qwen2.5vl` chưa có trên ổ rời.
- PRODUCT_ROADMAP đánh dấu đã làm: P2.3 (D19), P3.1 và P3.3 (D20), P3.4 (D21), P3.5 (D22). STATE: thêm lần kiểm thử Windows 26/10 (15/15 đạt), chuyển mục Wan 2.2 vào bảng nợ kỹ thuật.
- Nhánh `review-fixes` đã commit (`ff6dade`) và đẩy lên GitHub `Banhpao/test` — commit đầu tiên kể từ 21/8.
- Dùng thật lần đầu qua `/app` (19 lượt chat + 2 ảnh SD1.5 — lần đầu vẽ thật trên RTX 3050): kết quả và 6 lỗi tìm thấy ghi ở TESTLOG 27/10; STATE thêm "Trích fact lẫn nhân vật với người dùng" vào bảng chặn hướng sản phẩm; MAY.md ghi số đo ảnh. Không đổi mã.
- Sửa 6 lỗi của lượt dùng thật 27/10 (chưa chạy lại trên máy):
  - Trí nhớ (D25): bản ghi gửi model gọi rõ `NGƯỜI DÙNG` / `<tên> (nhân vật)`; bộ khoá fact cố định `FACT_KEYS` có nhãn tiếng Việt, khoá quen thuộc quy về khoá chuẩn; mỗi fact phải có `evidence` và khớp một câu khẳng định của người dùng (câu hỏi, câu nhờ vả không tính; giá trị chứa tên nhân vật bị bỏ). Tóm tắt hội thoại biết tên nhân vật và bỏ dòng "Chưa có". Trên dữ liệu 27/10: 7 fact sai bị bỏ, fact đúng giữ nguyên.
  - Định tuyến (D26): câu hỏi có chữ "vẽ" ("Pao đang vẽ gì vậy?") là trò chuyện; "vẽ cho anh… được không?" vẫn là xin ảnh.
  - `/app`: đang trả lời / đang vẽ thì dòng gợi ý dưới ô chat nói rõ và nháy khi nhấn Enter, chữ gõ vẫn giữ; bảng "… nhớ về bạn" tự tải lại 8 s, 30 s, 75 s sau mỗi lượt (bỏ qua khi đang sửa) và hiện nhãn tiếng Việt từ `labels` mới của `/api/memory`.
  - Pao (D27): few-shot "B-baka!" → "Baka!", lời chào bỏ "H-hừ"; chỉ chêm Baka/Urusai/Mou, cấm tiếng Anh, từ bịa, nói lắp, xưng "mình"/"tôi".
  - Ảnh nhân vật (D27): persona thêm `scene` và `image_lines`; tên nhân vật bị gỡ khỏi câu đưa đi dịch và khỏi prompt ra (`1pao` → `1girl`); `enrich_for_create_ex(..., persona)` dặn một cô gái, bối cảnh quen thuộc, hành động thấy rõ (Gradio không truyền nên giữ nguyên). Ảnh xin trong khung chat có câu của nhân vật lưu thành lượt chữ ngay trước ảnh, response có `caption`; chú thích dưới ảnh ghi "Bạn xin: …".
  - Test: `test_memory.py` 20, `test_chat_context.py` 16, `test_router.py` thêm mục 3b và 16, `test_chat_image_route.py` thêm 3, `test_api_storage.py` kiểm `labels`.

---

## 2026-10-26

- Model chat mặc định đổi sang `qwen3:4b-instruct` (D24): `yumeai/core/config.py`, env.example, RUNBOOK; `khoi_dong.py` lấy mặc định từ `config.py` thay vì tự ghi tên model; `check_chat_model()` gợi ý thêm qwen3; `test_router.py` mục 14 kiểm mặc định mới. Model dịch / tóm tắt vẫn tự dò như cũ.
- So model chat trên laptop (STATE việc 5, TESTLOG 26/10): `qwen3:4b-instruct` giữ vai 23/24, nhớ đúng 3/4; `qwen2.5:3b` 17/24, 0/4 nhưng nhanh gấp ~4 lần (64 so với 14.9 token/giây trên GPU 4 GB).
- `bench_chat.py`: luật "Không nhận AI" bắt nhầm đại từ "ai" ("…không hỏi tên tao là ai") — `AI` giờ chỉ khớp chữ HOA. `test_bench_chat.py` thêm hai câu đó.
- Mở app bằng một cú bấm (D23): `MO-PAOCAKE.cmd` gọi `project/khoi_dong.py` — bật Ollama trên ổ rời (`<ổ>\Ollama`, cổng 11435, model trên ổ nên máy nào cũng như nhau) hoặc dùng `OLLAMA_URL`/Ollama cài trên máy, bật ComfyUI trong cửa sổ thu nhỏ (`--lowvram` khi GPU < 10 GB; ≥ 12 GB thì VRAM bình thường + `YUMEAI_LOW_VRAM_MODE=0`), chạy `main.py` (hoặc `--gradio`), mở `/app` khi sẵn sàng; đóng thì tắt đúng những gì nó đã bật (kể cả bấm X). Báo rõ khi thiếu Ollama hay thiếu model đang cấu hình. `--model`, `--khong-comfy`, `--kiem-tra`. Test mới `test_khoi_dong.py` 22/22 (Ollama giả bằng HTTP server nhỏ).
- RUNBOOK: mục "Mở bằng một cú bấm"; "So sánh model chat" chạy với Ollama trên ổ rời; bảng "Chuyển dự án sang máy khác" cập nhật `.venv` portable và Ollama trên ổ rời.
- `test_storage.py` đóng kết nối sqlite bằng `contextlib.closing` trong hai test migration (v1, v2). `with sqlite3.connect()` chỉ commit chứ không đóng, nên trên Windows file `.db` còn bị giữ và `TemporaryDirectory.cleanup()` báo `WinError 32` — lỗi chỉ lộ ra khi chạy thật trên Windows (Linux cho xoá file đang mở). Cùng kiểu sửa đã làm cho `test_data_controls.py`, `test_rename_persona.py`.
- `project\.venv` được tạo lại từ Python portable 3.11.16 trên ổ rời (trước trỏ vào `C:\Users\tinha\...\Python311` của một máy khác nên hỏng ở mọi máy còn lại), cài đúng 172 thư viện + phiên bản của môi trường cũ (gồm torch 2.12.1+cu130 và thư viện ComfyUI). Chạy thử trên Windows: 13 file test offline đạt, ComfyUI `--quick-test-for-ci` nạp đủ 8 custom node, `pip check` sạch, torch thấy GPU. Môi trường cũ cất ở `E:\duan\_backup_claude\moi-truong-cu\`.

---

## 2026-10-22

- `bench_chat.py` so sánh model chat trên máy thật (STATE việc 5): cùng một bộ thoại cố định cho Pao và Su (6 câu mỗi nhân vật, có câu dụ nhận là AI, dụ gạch đầu dòng, hỏi bằng tiếng Anh, hỏi lại trí nhớ), dựng prompt bằng `build_persona_messages()` + `chat_options()` thật, bắt đầu bằng lời chào như hội thoại mới, đưa câu trả lời lại làm lịch sử. Đo nạp cold, bộ nhớ và % trên GPU (`/api/ps`), token/giây, chờ chữ đầu, cả câu; chấm giữ vai bằng luật (tiếng Việt, chữ Hán, emoji, liệt kê, nhận AI, xưng hô, độ dài/bị cắt, nhớ đúng). Không tải model (in lệnh `ollama pull`), không đụng DB/ComfyUI, từ chối khi ComfyUI đang vẽ; nhả model trước/sau mỗi model (`--no-free` để bỏ). Ghi `data/bench/chat_*.json` (kèm mọi câu trả lời) + khối TESTLOG. Dùng lại hàm dò máy của `bench_baseline.py`. Test mới `test_bench_chat.py` 20/20 (Ollama/ComfyUI giả bằng `httpx.MockTransport`).
- Số đo hiệu năng thật (D22, lộ trình P3.5): mỗi lượt `/api/chat` ghi token/giây, số token prompt, thời gian Ollama nạp model (từ chunk cuối), thời gian dựng ngữ cảnh, chờ chữ đầu và cả lượt; mỗi ảnh `/api/images/generate` ghi thời gian dịch/viết prompt, ComfyUI vẽ, cả lượt, preset, model, kích thước. Nối vào `data/perf.jsonl` qua `yumeai/core/perf.py` (tự xoay vòng ~2 MB) — chỉ số, không nội dung, không user_id. `GET /api/stats` trả trung vị theo model và theo preset; `YUMEAI_PERF_LOG=0` để tắt. Response ảnh thêm trường `timing`. `/app`: màn Tạo ảnh ghi thời gian thường lệ đo trên máy này cho đúng mức chất lượng, đồng hồ lúc vẽ thêm "thường khoảng mm:ss". "Xoá toàn bộ dữ liệu" xoá luôn sổ đo (vì có giờ từng lượt). Test mới `test_perf.py` 19/19; `test_api_storage.py`, `test_chat_image_route.py` trỏ sổ đo vào thư mục tạm để không ghi vào `data/` thật. Bấm thử bằng Chromium: 17 nhóm đạt, sổ đo không chứa chữ nào của tin nhắn hay prompt.
- DECISIONS D21: bỏ câu gợi ý gọi `sao_luu.py` từ một dự án khác; muốn chạy theo lịch thì dùng Task Scheduler của Windows.
- Sao lưu / khôi phục (D21, lộ trình P3.4): `sao_luu.py` gói DB (backup API sqlite, đúng cả khi đang chạy WAL) + ảnh + manifest vào `data/backups/sao-luu-*.zip`; `--danh-sach`; `--khoi-phuc` từ chối khi main.py/app_gradio.py đang chạy, kiểm integrity + schema, hỏi lại, luôn lưu dữ liệu hiện tại trước, xoá -wal/-shm cũ. Chỉ thư viện chuẩn. Test mới `test_sao_luu.py` 11/11.
- Quyền kiểm soát dữ liệu (D20, lộ trình P3): màn chào lần đầu ở `/app` ghi rõ nhân vật AI và chỗ lưu dữ liệu; `DELETE /api/gallery/{id}` (xoá cả file và tin nhắn ảnh), `DELETE /api/conversations/{id}` (giữ ảnh và ký ức), `GET /api/data/export` (zip `data.json` + ảnh), `POST /api/data/delete` (gõ "XOÁ", xoá thật + VACUUM, không đụng dữ liệu người khác). UI: nút xoá ảnh, nút xoá cuộc trò chuyện, hộp "Dữ liệu của bạn", hộp hỏi lại trước khi xoá. Test mới `test_data_controls.py` 8/8; `test_rename_persona.py` đóng kết nối sqlite rõ ràng (trên Windows kết nối còn mở làm thư mục tạm không xoá được). Bấm thử bằng Chromium: 17 nhóm thao tác đạt.
- Xin ảnh ngay trong khung chat của `/app` (D19 mục 7): `POST /api/chat/intent` dùng chung `plan_request()` với Gradio để phân biệt trò chuyện và xin ảnh; `ImgReq.chat_message` lưu câu xin ảnh ngay trước ảnh, chỉ khi vẽ xong. Trong chat hiện thẻ "đang vẽ" có đồng hồ, xong ảnh hiện tại chỗ; ComfyUI tắt hoặc bộ vẽ bận thì báo rõ và trả lại chữ vào ô nhập. Test mới `test_chat_image_route.py` 8/8; bấm thử bằng Chromium: 12 nhóm thao tác đạt.
- Làm lại giao diện `/app` theo bản thiết kế "Ngân hà hồng" (D19): bốn màn Trò chuyện · Tạo ảnh · Bộ sưu tập · Chọn nhân vật đổi bằng `#hash`; nền ngân hà tối dần xuống hồng dịu; ký ức thêm/sửa/xoá ngay trong bảng; màn Tạo ảnh có chọn nhân vật, kiểu ảnh, 4 mức chất lượng (mặc định Tốt nhất), phóng to, viết lời tả chi tiết, gửi ảnh vào hội thoại đang mở; Bộ sưu tập lọc theo nhân vật, xem chi tiết, vẽ lại với lời tả cũ; chọn nhân vật tạo hội thoại mới có lời chào. Màn hẹp ≤ 900px: thanh bên và hồ sơ thành ngăn kéo. Bộ gõ tiếng Việt: Enter lúc đang ghép chữ không gửi tin.
- Test mới `test_web_ui.py` 9/9: UI chỉ gọi route có trong `main.py`, giá trị chất lượng/kiểu ảnh/phóng to khớp `comfy_service`, mặc định là mức kỹ nhất, `innerHTML` chỉ cho icon hằng số, `/app` trả đúng file. Đã bấm thử bằng Chromium với Ollama/ComfyUI giả và DB cũ còn id yuki/hana: 10 nhóm thao tác đạt, không lỗi console; DB tự đổi sang Pao/Su lúc khởi động.
- Đổi tên nhân vật Yuki → Pao và Hana → Su (D18), cả tên hiển thị lẫn id (`yuki` → `pao`, `hana` → `su`); tính cách, xưng hô, ngoại hình giữ nguyên. `personas.py` thêm `PERSONA_ID_ALIASES` + `resolve_persona_id()`; `main.py` gom `persona_id` của 4 request model vào `_PersonaRef` (validator nhận id cũ), hai endpoint query `/api/memory` cũng nhận id cũ; Gradio và `chat-ui-mockup.html` đổi mặc định/nhãn.
- `YumeDatabase.initialize()` nhận thêm `renamed_ids`; `rename_characters()` chuyển hội thoại, trí nhớ, ảnh của mọi id cũ sang id mới trong một transaction, một bản sao lưu trước vào `data/backups/truoc-doi-ten-yuki-thanh-pao-hana-thanh-su-*.db`, chỉ đổi tiêu đề tự sinh. DB mới hoặc đã chuyển thì không làm gì.
- Test mới `test_rename_persona.py` 11/11 (chuyển dữ liệu + `foreign_key_check`, chỉ đổi tiêu đề tự sinh, một bản sao lưu còn dữ liệu cũ, lỗi giữa chừng thì không đổi dở, DB chỉ có một id cũ, trùng khoá trí nhớ, DB mới không sao lưu, API nhận id cũ, mặc định là Pao). Test cũ đổi sang `pao`/`su`: storage 13, memory 11, chat_context 15, api_storage 7, router, bench_baseline 48 — đều pass; ruff (F) sạch.

---

## 2026-10-21

- Giai đoạn 0 (D17): thêm `bench_baseline.py` đo nền trên máy thật — GPU/VRAM (nvidia-smi), RAM, chỗ trống ổ chứa `ComfyUI/models` (cần ≥ 30 GiB), N job text2img SD1.5 đi đúng đường REST qua `run_comfy_job` (job 1 cold sau `/free`, còn lại warm; VRAM đỉnh lấy mẫu 0.5s; thời gian ComfyUI từ `/history`; giá nhả VRAM Ollama D16). Ghi `data/bench/pc0_baseline_*.json` + khối dán TESTLOG. Không khởi động dịch vụ, không đổi cấu hình, không ghi DB/gallery; prompt cố định, không persona.
- Test offline `test_bench_baseline.py` 48/48 (chạy được không cần venv): nvidia-smi thiếu/hỏng/rác, ngưỡng đĩa, sampler VRAM, cold/warm, JSON + khối TESTLOG, ComfyUI/Ollama tắt, hàng đợi/Resource Guard bận, job lỗi giữa chừng, KSampler lấy từ cache, mặc định REST đọc từ `main.py` bằng ast.
- DECISIONS: D17 thay một phần D9 — cho phép video ngắn từ ảnh persona, chỉ REST + `/app`, chỉ bật khi hồ sơ máy cho phép; vẫn không làm dịch vụ credit/cloud. RUNBOOK thêm mục "Đo nền (giai đoạn 0)"; STATE thêm việc 0.

---

## 2026-10-15

- Sửa tiếp 3 lỗi cụ thể:
  - `clean_history()` nhận content dạng list khối `{"type": "text"}` của Gradio 6 (bản cũ bỏ sạch các lượt đó khi Gradio không đọc được DB).
  - Fact extraction: Ollama < 0.5 trả HTTP 400 khi `format` là JSON schema → thử lại một lần với `format: "json"` (trước đây trên Ollama cũ không lô nào trích được fact).
  - `test_summary_cache_does_not_leak_between_conversations` dùng đủ một lô D13 và kiểm cả cache trúng đúng hội thoại; `test_api_storage.py` 7/7. Test mới: router (Gradio 6), memory 2.
- Sửa 7 lỗi cụ thể (audit 2026-10-09), không đổi kiến trúc:
  - VRAM (D16): trước mỗi job ComfyUI nhả model Ollama khỏi GPU (`keep_alive=0`, trong `run_comfy_job`); trước mỗi lời gọi vision nhờ ComfyUI `/free`. Tắt bằng `YUMEAI_VRAM_HANDOFF=0`. Thêm `YUMEAI_OLLAMA_NUM_GPU` (gửi `options.num_gpu` cho mọi lời gọi Ollama; trống = như cũ).
  - Router Gradio: câu chat không ảnh không còn chờ planner LLM (chỉ hỏi khi câu nhắc tới ảnh/hình/chụp); `smart_execute` nhận lại plan đã tính thay vì lập kế hoạch lần hai; nhánh không ảnh ra `chat` thì trả lời như chat thay vì "Xong! Task: chat".
  - Fact extraction: `num_ctx` = `CHAT_NUM_CTX` (hết nạp lại model), transcript tính theo num_ctx và giữ phần MỚI theo ranh giới message, `num_predict` 600, JSON schema trong `format`, JSON hỏng 3 lần liền thì bỏ qua lô thay vì kẹt mãi.
  - `main.py` bỏ CORS `*` (UI `/app` cùng origin, không cần CORS).
  - Model chat mặc định `qwen2.5:3b` như env.example/RUNBOOK; thêm `YUMEAI_VISION_MODEL`, `YUMEAI_TRANSLATE_MODEL`; gửi `think: false`; dò model tí hon bằng regex (`:11b` không còn bị coi là `1b`).
  - Tin nhắn người dùng ghi cùng câu trả lời (hết lượt user mồ côi khi Ollama lỗi) ở cả REST và Gradio; chỉ đọc cửa sổ lịch sử cần dùng từ DB (`history_load_limit`, ranh giới lô khớp D13).
  - `requirements.txt` ghim đúng bộ version đang chạy trong `.venv`.
- Test mới: router +3 nhóm (13–15), storage 13, chat_context 15, memory 9, API 7 (1 test cũ vẫn fail từ trước, xem STATE).

---

## 2026-09-11

- Chuẩn bị Wan 2.2 TI2V 5B: tải 3 file Comfy-Org vào đúng thư mục ComfyUI/models; SHA-256 và safetensors đạt; thêm workflow chính thức và bản thử 512x288/17 frames, kiểm tra node và model discovery đạt. RTX 3050 4GB: chưa thử sinh video, chưa tích hợp video vào YumeAI. Chi tiết E:/Python/WAN22_YUMEAI.md và manifests/wan22-5b.json.

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
