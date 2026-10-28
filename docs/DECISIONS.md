# Nhật ký quyết định — YumeAI

Mỗi mục ghi lại **một quyết định đã chốt**: bối cảnh, chọn gì, và quan trọng nhất
là **đã loại bỏ hướng nào**. Mục đích là để người (hoặc AI) tiếp theo không đề xuất
lại phương án đã cân nhắc và bỏ.

Thêm mục mới ở **cuối file**, đánh số tiếp. Đừng sửa mục cũ — nếu quyết định thay
đổi thì viết mục mới ghi rõ "thay thế D-x".

---

## D1 — Sản phẩm là AI companion, không phải công cụ sửa ảnh
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt

**Bối cảnh.** Sau ~4 tháng phát triển, khoảng 85% code (`app_gradio.py`, ~3800
dòng) là pipeline sửa ảnh: garment transfer, mask+inpaint, multi-char 2-pass,
teacher plan, verify + count lock, rollback. Trong khi đó xương sống companion —
nhân vật cố định, trí nhớ, gallery — chưa có dòng nào.

**Quyết định.** Sản phẩm là **AI companion theo mô hình candy.ai**. Ba trụ theo
thứ tự: (1) nhân vật cố định, (2) trò chuyện có trí nhớ, (3) ảnh của chính nhân
vật đó. Sửa ảnh là **tính năng phụ**, không phải sản phẩm.

**Hệ quả.** Toàn bộ pipeline sửa ảnh hiện có được **giữ nguyên nhưng ngừng phát
triển thêm** cho tới khi ba trụ trên xong. Không thêm tính năng chỉnh sửa ảnh mới.

**Đã loại.** "Cứ hoàn thiện phần đổi đồ cho ngon rồi tính tiếp" — vì đó là bài
toán khó nhất và ít giá trị cốt lõi nhất trong ba thứ.

---

## D2 — Local-first: ComfyUI + Ollama, không dùng API trả tiền
**Ngày:** trước 2026-08 (quyết định gốc của dự án) · **Trạng thái:** đã chốt

**Quyết định.** Mọi thứ chạy local: ComfyUI cho ảnh, Ollama cho ngôn ngữ.

**Hệ quả.** Không phụ thuộc nhà cung cấp, không giới hạn nội dung, chi phí bằng 0.
Đổi lại: chậm, và bị giới hạn bởi phần cứng (xem D3).

**Ghi chú.** `env.example` có khai `ANTHROPIC_API_KEY` và `FAL_KEY` nhưng **không
code nào đọc chúng** và không có `python-dotenv` trong `requirements.txt`. Đây là
tàn dư của giai đoạn thăm dò, không phải phụ thuộc thật. Có thể xoá.

---

## D3 — Chấp nhận ràng buộc iGPU 4GB, không chờ phần cứng
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt

**Bối cảnh.** Máy phát triển dùng **AMD Radeon 680M** — GPU tích hợp, không có
VRAM riêng. "4GB VRAM" là RAM hệ thống bị cắt ra (UMA Frame Buffer): 16GB thực,
hệ điều hành chỉ thấy ~12.4GB.

**Quyết định.** Xây phần xương sống companion **ngay trên máy hiện tại**, không
chờ nâng cấp. Lý do: DB, trí nhớ, gallery, API, quản lý nhân vật **gần như không
đụng GPU**.

**Hệ quả.** Code phải luôn tôn trọng: không SDXL, chỉ 1 job ComfyUI cùng lúc,
cap steps Flux, timeout dài. Các chỗ này đều đã có công tắc môi trường
(`YUMEAI_LOW_VRAM_MODE`, `YUMEAI_FLUX_SAFE_STEPS`...) để nới ra khi có máy mạnh.
`_pick_best_quant()` tự chọn quant tốt nhất có trong `models/unet`, nên nâng máy
xong chỉ cần tải file Q5/Q6 vào, **không phải sửa code**.

**Chưa làm được trên máy này:** train LoRA nhân vật (cần ~8–12GB VRAM thật). Đây
là rào cản lớn nhất cho character consistency — cần GPU rời, không phải thêm RAM.

---

## D4 — Router mặc định là CHAT, không phải tạo ảnh
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt · **Sửa bug nghiêm trọng**

**Bối cảnh.** `rule_plan_request()` cũ: không có ảnh + không khớp `CREATE_HINTS`
lẫn `CHAT_HINTS` → fallback sang `text2img`. Kết quả đo được:

```
'hôm nay bạn thế nào?'  → text2img   ← đi vẽ tranh thay vì trả lời
'anh nhớ em không'      → text2img
'em ăn cơm chưa'        → text2img
```

Tệ hơn: LLM fallback chỉ chạy khi task ∈ {`generic_edit`, `chat`}, nên quyết định
`text2img` sai **không bao giờ được soi lại**.

**Quyết định.** Đảo mặc định: không có ảnh + không có yêu cầu tạo ảnh **rõ ràng**
→ `chat`. Nhận diện tạo ảnh chặt hơn: `"vẽ"` tự nó đủ; `"tạo"/"làm"` phải kèm danh
từ ảnh; thêm nhóm cụm xin ảnh (`"gửi ảnh đi"`, `"cho anh xem hình"`).

Thêm trường `confidence` vào `PromptPlan`. LLM fallback nay chạy khi rule **không
chắc** (`confidence == "low"`), thay vì theo tên task.

---

## D5 — Khớp từ khoá theo ranh giới từ, không dùng `in`
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt · **Sửa bug nghiêm trọng**

**Bối cảnh.** Toàn bộ router dùng `keyword in text`. Với tiếng Việt điều này hỏng
nặng:

```
"hi" khớp trong: khi, chi, hiểu, nhiều, hình
"đồ" khớp trong: đồng ý, đồng phục, đồi
```

Đo được: `'tôi đồng ý thay đổi kế hoạch'` → bị coi là lệnh đổi trang phục → đẩy
vào Flux Kontext, chạy ~400 giây cho một câu chat.

**Quyết định.** `contains_word()` trong `prompt_router.py`, dùng
`(?<!\w)keyword(?!\w)`. Python `\w` là Unicode-aware nên tiếng Việt có dấu được
tính là ký tự chữ, hoạt động đúng.

**Bắt buộc.** Mọi chỗ dò từ khoá tiếng Việt phải dùng hàm này. Không dùng `in`.

---

## D6 — Một nguồn duy nhất cho mọi logic dùng chung
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt

**Bối cảnh.** `app_gradio.py` và `main.py` mỗi bên tự định nghĩa lại persona,
prompt, preset, xử lý lịch sử. Kết quả: cùng một input, hai cửa vào cho hai kết
quả khác nhau.

Ví dụ cụ thể đã tìm thấy:

| | app_gradio cũ | main.py cũ |
|---|---|---|
| Ngoại hình nhân vật trong prompt | không có | có |
| Style tag | có | không có |
| Negative prompt | `jpeg artifacts` | `mutated hands` |
| Lịch sử chat | có (nhưng lọc sai) | **không có gì** |
| Timeout chat | 30s | 120s |
| Chuỗi upscale | `"Không upscale"` | `"Không"` |

**Quyết định.** Mọi logic dùng chung nằm trong `yumeai/`. Entrypoint chỉ được làm
hai việc: nhận input, và đổi kiểu lỗi cho hợp framework (`gr.Error` /
`HTTPException`). `yumeai/` **không được import gradio hay fastapi**.

**Hệ quả.** Đã tạo `chat_context.py`, `image_prompt.py`, `persona_prompt.py`, và
`normalize_quality()`/`normalize_upscale()` trong `comfy_service.py`.

---

## D7 — Persona giàu chi tiết + few-shot, vì model local nhỏ
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt

**Bối cảnh.** Persona cũ là 4 dòng system prompt. Đủ cho Claude/GPT (model lớn tự
suy ra phần còn lại), **không đủ** cho model local 3–8B.

**Quyết định.** Hồ sơ nhân vật đầy đủ: `personality`, `speech_style`, `quirks`,
`likes`/`dislikes`, `boundaries`, `greeting`, `examples`. System prompt tự dựng từ
đó (230 → ~1810 ký tự), kèm **few-shot** 4–5 lượt thoại mẫu chèn trước lịch sử.

Trong `examples` luôn có một cặp trả lời câu "em là AI à?" để nhân vật không tự khai.

**Nguyên tắc chung rút ra:** model càng nhỏ càng cần hướng dẫn cụ thể và ví dụ mẫu.
Đừng viết prompt cho model local như viết cho GPT-4.

---

## D8 — Chưa có database — nợ kỹ thuật đã biết, ưu tiên cao nhất
**Ngày:** 2026-08-18 · **Trạng thái:** ĐANG NỢ

**Hiện trạng.** Không có DB. Trí nhớ hội thoại chỉ tồn tại trong phiên (Gradio
`State`, hoặc client tự giữ và gửi lên qua `ChatReq.history`). Đóng app là mất
sạch. Không có gallery ảnh, không có user, không có lịch sử nhân vật.

`chat_context.summarize_old_history()` có nén các lượt cũ thành trí nhớ dài, nhưng
**cũng chỉ trong phiên**.

**Quyết định.** Chấp nhận tạm, nhưng đây là **việc tiếp theo cần làm**. Thiết kế
đã tính trước: `build_persona_messages()` nhận `memory_summary` từ ngoài, nên khi
có DB chỉ cần đọc từ bảng thay vì tính lại.

**Kế hoạch.** SQLite, các bảng: `user`, `character`, `conversation`, `message`,
`memory_fact`, `image`.

---

## D9 — Không làm image-to-video (mô hình MotionMuse)
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt — loại bỏ · **Thay một phần bởi D17** (2026-10-21)

**Bối cảnh.** Có cân nhắc hướng giống motionmuse.ai (NSFW image-to-video, mô hình
credit, ~16.6 triệu lượt truy cập/tháng).

**Quyết định.** **Không theo.** Lý do:

1. Dự án hiện **không có chút năng lực video nào** — kiểm tra `ComfyUI/models`
   không có WAN/SVD/AnimateDiff/Hunyuan, `custom_nodes` toàn node ảnh tĩnh.
2. Model video cần **tối thiểu 8–12GB VRAM thật**, iGPU 4GB không chạy nổi.
3. Mô hình tool đòi **tốc độ là sản phẩm** (vài chục giây/clip). Flux hiện tại mất
   ~400 giây/ảnh.
4. Tool phục vụ nhiều người ⇒ cloud GPU + queue + billing — **ngược hoàn toàn** với
   D2 (local-first).

**Ghi chú quan trọng.** Về *mô hình sản phẩm*, code hiện tại (tool stateless: vào
ảnh → ra ảnh) thực ra gần MotionMuse hơn là gần candy.ai. Đây chính là lý do phải
có D1 — nếu không nói rõ, dự án sẽ tự trôi về hướng tool.

---

## D10 — Tài liệu bộ nhớ dự án nằm trong repo, không nằm trong bộ nhớ của AI
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt

**Bối cảnh.** Chủ dự án làm việc luân phiên với nhiều AI (Claude, GPT) và di chuyển
dự án giữa các máy bằng ổ cứng rời. Mỗi phiên mới lại phải giải thích lại từ đầu.

**Quyết định.** Toàn bộ ngữ cảnh dự án nằm trong repo dưới dạng file markdown
(`AGENTS.md`, `CLAUDE.md`, `docs/`), **không** dựa vào bộ nhớ riêng của bất kỳ AI
nào — vì bộ nhớ đó không mang theo được sang công cụ khác.

**Hệ quả.** Ai (hoặc AI) nào cũng phải cập nhật tài liệu khi thay đổi. Xem mục 8
trong `AGENTS.md`.

---

## D11 — SQLite là nguồn dữ liệu sản phẩm; hoãn framework memory nặng
**Ngày:** 2026-08-21 · **Trạng thái:** đã chốt · **Thay thế nợ D8**

**Bối cảnh.** Chat/persona đã chạy nhưng history nằm ở client/Gradio State, ảnh
REST chỉ trả base64 và UI dùng conversation/credit/affection giả. Đóng app là mất
toàn bộ trạng thái sản phẩm. Có cân nhắc ghép SillyTavern hoặc Mem0/Qdrant.

**Quyết định.** Dùng SQLite trong thư viện chuẩn làm nguồn dữ liệu local duy nhất.
Schema v1 có `users`, `characters`, `conversations`, `messages`, `memory_facts`,
`images`. Web UI `/app` đi qua FastAPI conversation/gallery API; Gradio tiếp tục
là cửa debug pipeline ảnh cho tới khi pipeline được tách khỏi framework.

**Vì sao.** Máy chỉ có khoảng 12GB RAM khả dụng và phải chạy Ollama + ComfyUI.
Một server memory/vector DB riêng làm tăng RAM, vận hành và điểm hỏng trước khi có
đủ dữ liệu để chứng minh semantic retrieval cần thiết. SQLite cũng dễ backup khi
di chuyển dự án bằng ổ ngoài.

**Hệ quả.**

- Dữ liệu mặc định ở `project/data/`, được gitignore; đổi ổ bằng
  `YUMEAI_DATA_DIR`/`YUMEAI_DATABASE_PATH`/`YUMEAI_GENERATED_IMAGES_DIR`.
- Client gửi `conversation_id`; server tự đọc lịch sử và lưu cả hai phía chat.
- Ảnh REST lưu PNG + metadata và trả `image_url`, vẫn giữ base64 để tương thích.
- `user_id` chỉ là phân vùng dữ liệu local, **không phải authentication**. Không
  được expose Internet trước khi có auth, rate limit và CORS chặt.
- FTS5/Ollama embeddings chỉ thêm sau khi benchmark. Mem0/Qdrant không bị cấm
  vĩnh viễn, nhưng phải có số liệu chứng minh SQLite không đủ.

**Sửa kèm.** Cache summary cũ dùng `(persona_name, bucket)`, có thể trả trí nhớ của
conversation khác có cùng persona/số message. Cache nay thêm fingerprint nội dung.

---

## D12 — Fact memory chạy theo batch sau response, không chạy mỗi message
**Ngày:** 2026-08-21 · **Trạng thái:** đã chốt

**Bối cảnh.** Lưu toàn bộ history giải quyết mất dữ liệu nhưng model nhỏ vẫn cần
fact ngắn để nhớ xuyên nhiều conversation. Gọi extractor sau mọi lượt sẽ làm máy
Radeon 680M/~12GB RAM phải sinh hai lần cho mỗi câu và UI có cảm giác chậm gấp đôi.

**Quyết định.** Mặc định gom 12 text message rồi trích một batch tối đa 24 message,
context 2048 và tối đa 8 fact. Job chạy bằng Starlette background task sau khi SSE
đã hoàn tất. SQLite giữ con trỏ message đã xử lý; lỗi thì cooldown 5 phút.

**Quyền riêng tư.** Auto extractor bỏ các key về mật khẩu, OTP, API key, tài khoản
ngân hàng, giấy tờ định danh và vị trí chính xác. UI luôn cho xem, thêm, sửa và xoá
fact. Fact là phạm vi user–persona để nhân vật nhớ xuyên conversation.

**Đã loại.**

- Chạy extractor mỗi lượt: tốn tài nguyên và tăng latency không cần thiết.
- Đưa Mem0/Qdrant vào ngay: thêm service/RAM khi số fact còn quá nhỏ.
- Embedding mọi message: chưa có benchmark chứng minh tốt hơn fact có cấu trúc.

---

## D13 — Khoá cache tóm tắt phải bằng đúng thứ gửi cho LLM
**Ngày:** 2026-08-21 · **Trạng thái:** đã chốt · **Sửa bug hiệu năng nghiêm trọng**

**Bối cảnh.** `summarize_old_history()` băm TOÀN BỘ `overflow` để làm khoá
`_SUMMARY_CACHE`. Overflow đổi nội dung mỗi lượt chat, nên khoá đổi mỗi lượt và
cache **không bao giờ trúng** — trái hẳn ý đồ của `CHAT_SUMMARY_EVERY`.

Tệ hơn: `summarize_conversation()` cắt transcript bằng `[:6000]` — giữ 6000 ký tự
**đầu**. Đo được: từ khoảng lượt 50 trở đi input gửi vào LLM **giống hệt nhau từng
byte**. Tức mỗi lượt chat trả giá một lời gọi LLM để tính lại đúng kết quả cũ, rồi
ném đi. Đo trên 60 lượt: **54 lời gọi**.

Cùng lúc, phần hội thoại **mới nhất không bao giờ lọt vào tóm tắt** — trí nhớ dài
đóng băng ở đoạn đầu cuộc trò chuyện, im lặng, không lỗi.

Từ 2026-08-21 endpoint `/api/chat` gọi thẳng hàm này mỗi lượt, nên chi phí nằm ngay
trước token đầu tiên người dùng nhìn thấy.

**Quyết định.**

1. **Khoá cache băm đúng cửa sổ thật sự gửi đi**, không băm dữ liệu mà hàm bên dưới
   sẽ vứt. Nguyên tắc chung: *khoá cache không được đổi theo phần dữ liệu mà callee
   loại bỏ.*
2. Cửa sổ **ổn định trong một lô**: cắt ở ranh giới `bucket * CHAT_SUMMARY_EVERY`.
   Chưa đủ một lô thì chưa tóm tắt (chậm vài lượt, không mất gì).
3. Cửa sổ **có cận trên** `CHAT_SUMMARY_MAX_MESSAGES` (mặc định 40) và **trôi theo
   phần mới**, thay vì phình vô hạn rồi bị cắt mất đuôi.
4. `summarize_conversation()` cắt từ **đầu** transcript theo ranh giới dòng, giữ
   phần mới nhất.
5. **Không cache chuỗi rỗng.** Ollama chết cũng trả `""`; cache lại là khoá cứng
   "không có trí nhớ" cho tới hết phiên dù dịch vụ đã sống lại.
6. `main.py` **ghi tóm tắt xuống `conversations.memory_summary`**. Trước đó cột này
   được đọc nhưng không nơi nào ghi. Bản đã lưu dùng làm lưới đỡ khi Ollama chết
   hoặc vừa mở lại app — **không ghép** với bản vừa tính, vì hai bản gần trùng nhau,
   ghép lại là nhét trí nhớ hai lần vào system prompt.

**Kết quả đo.** 60 lượt chat: **54 → 18** lời gọi LLM. Payload: không giới hạn →
tối đa 40 message.

**Đã loại.** Thêm `user_id` vào khoá cache (giải quyết rò rỉ giữa hai user nhưng
không giải quyết việc miss mỗi lượt). Bỏ hẳn cache (mỗi lượt một lời gọi LLM trên
máy CPU là không dùng được).

**Còn nợ.** Cửa sổ trôi nên fact từ đầu hội thoại rơi ra khỏi tóm tắt sau ~40
message overflow. Đường bền vững là `memory_facts` (D12) — nhưng **chỉ có ở REST**,
`app_gradio.py` chưa nối DB. Muốn đúng hẳn thì cần tóm tắt cuộn (summary cũ + phần
mới → summary mới); chưa làm vì cần đổi cả prompt của `summarize_conversation()`.

---

## D14 — Ảnh thuộc về người dùng, không thuộc về hội thoại
**Ngày:** 2026-08-21 · **Trạng thái:** đã chốt · **Sửa bug mất dữ liệu**

**Bối cảnh.** `list_images()` lọc theo user bằng `JOIN conversations`. Đó là INNER
JOIN, nên mọi ảnh có `conversation_id IS NULL` bị loại. Mà `/api/gallery` **luôn**
truyền `user_id`, còn `ImgReq.conversation_id` lại là **tuỳ chọn**.

Hệ quả đo được: sinh ảnh không kèm `conversation_id` → ảnh nằm trong DB và trên đĩa
nhưng **không đường nào xem lại được**. Với ảnh mất ~400 giây/tấm thì đây là mất
dữ liệu thật.

**Quyết định.** Schema v3: thêm `images.user_id`, lọc thẳng trên cột đó, bỏ JOIN.
`record_image()` suy ra chủ từ hội thoại khi không được truyền `user_id`, để ảnh
không bao giờ rơi vào trạng thái không có chủ.

**Migration.** Backfill từ `conversations`; ảnh mồ côi thì gán cho người dùng duy
nhất nếu DB chỉ có một. `user_id = ''` (không suy ra được) vẫn cho hiện — bản local
chưa có đăng nhập, giấu mất ảnh của người dùng tệ hơn.

**Đã loại.** `LEFT JOIN ... OR i.conversation_id IS NULL` — chữa triệu chứng nhưng
giữ nguyên mô hình sai "ảnh thuộc về hội thoại", và làm ảnh mồ côi hiện với mọi user.

---

## D15 — Gradio dùng chung DB với REST; khối trí nhớ nằm ở một chỗ
**Ngày:** 2026-08-21 · **Trạng thái:** đã chốt

**Bối cảnh.** Sau khi có SQLite (D11), REST nhớ được qua nhiều phiên còn Gradio vẫn
giữ toàn bộ hội thoại trong `gr.State` — đóng app là mất sạch. Hai cửa vào nhớ hai
kiểu khác nhau đúng là thứ D6 sinh ra để chặn. Thêm nữa `app_gradio.py:3489` gọi
`build_persona_messages()` **thiếu `user_name`**, nên qua Gradio nhân vật chưa bao
giờ biết tên người dùng còn qua REST thì biết.

**Quyết định.**

1. `app_gradio.py` mở **cùng một file SQLite** với `main.py`. Lịch sử đưa vào prompt
   lấy từ **DB**, không từ `gr.State`.
2. **Khối trí nhớ nằm ở một chỗ duy nhất**: `memory_service.build_conversation_memory()`.
   Cả hai entrypoint gọi nó. `main.py` đã được sửa để dùng lại hàm này thay vì bản
   copy trong endpoint. Nguyên tắc D6 áp cho *ngữ cảnh chat* y như đã áp cho prompt
   ảnh và preset.
3. Gradio dùng **một người dùng ngầm định** `DEFAULT_USER_ID` (mặc định `user_001`,
   trùng mặc định của `ChatReq.user_id`). **Không thêm ô nhập tên** — thêm input UI là
   phải sửa cả `shared_inputs`, chữ ký `smart_unified_chat` và cái `assert` ở cuối file.
4. **Lỗi lưu trữ không được giết cuộc trò chuyện.** Mọi lời gọi DB trong `app_gradio.py`
   đi qua `_db_try()`: hỏng thì in cảnh báo **nói rõ đang hỏng việc gì** rồi chạy tiếp
   không có trí nhớ. Gradio còn là cửa debug pipeline ảnh; mất trí nhớ khó chịu, mất
   luôn khả năng chat thì tệ hơn.
5. Trích fact chạy **thread nền** sau khi trả lời xong, giống `BackgroundTask` bên REST.

**Đã loại.**

- **Nạp lại khung chat từ DB khi mở app.** Cần `demo.load()` và đụng vào
  `_pack_main_outputs` — hàm này dùng chung với `auto_outfit_handler`, mà
  `auto_btn.click` khai outputs 5 phần tử. Khung chat vẫn trống khi mở lại, nhưng
  **nhân vật thì nhớ** — đó mới là thứ người dùng cảm nhận được.
- **`conversation_id` làm `gr.State`.** Không có đường nào để `_pack_main_outputs`
  biết `conv_id`; để mặc định `None` thì mỗi lần yield sẽ ghi `None` đè lên State,
  triệu chứng là "thỉnh thoảng nhân vật quên". Thay bằng
  `get_or_create_conversation(user_id, persona_id)` gọi ở đầu mỗi lượt.
- **Đổi chữ ký `_stream_persona_chat`.** Không cần: `persona_id` đã có sẵn trong tham
  số, nên hai chỗ gọi (3619, 3669) không phải sửa dòng nào.

**Còn nợ.** Ảnh sinh từ Gradio chưa được ghi vào bảng `images` — gallery bền vững
hiện chỉ có ở đường REST. Cần hook ở `runtime.run_comfy_job` (xem lộ trình giai đoạn D).

---

## D16 — Ollama và ComfyUI nhường VRAM cho nhau trên GPU 4GB
**Ngày:** 2026-10-15 · **Trạng thái:** đã chốt, **chưa chạy thử trên RTX 3050**

**Bối cảnh.** Mọi quyết định phần cứng trước đây dựa trên iGPU Radeon 680M, nơi Ollama
chạy RAM/CPU và "không đụng VRAM" (AGENTS.md mục 5). Máy hiện tại là RTX 3050 Laptop
4GB: Ollama đẩy model lên GPU và giữ 5 phút sau mỗi lời gọi. Một yêu cầu ảnh bình
thường là dịch/enrich (Ollama) rồi ngay lập tức job ComfyUI; bước verify bằng vision
lại chạy ngay sau job Flux. Không chỗ nào điều phối hai bên.

**Quyết định.** Một chính sách, hai điểm móc, một công tắc `YUMEAI_VRAM_HANDOFF`
(mặc định bật):

1. `runtime.run_comfy_job()` — sau khi đã giữ slot Resource Guard, trước khi gửi job —
   gọi `ollama_service.unload_ollama_models()`: đọc `/api/ps`, gửi `keep_alive: 0` cho
   model nào đang có `size_vram > 0`. Mọi job ComfyUI của cả hai cửa vào đều đi qua hàm
   này nên chỉ cần móc một chỗ.
2. Trước mỗi lời gọi vision trong `app_gradio.py`: `comfy_service.free_comfy_vram()` →
   `POST /free {"unload_models": true, "free_memory": true}`, timeout 3s, lỗi thì bỏ qua.

Cả hai đều best-effort: dịch vụ tắt/lỗi chỉ in cảnh báo, không chặn chat hay job vẽ.
Thêm `YUMEAI_OLLAMA_NUM_GPU` (gửi `options.num_gpu` ở **mọi** lời gọi Ollama — khác
nhau giữa hai lời gọi cùng model là Ollama nạp lại). Đặt `0` thì Ollama chạy hẳn CPU;
khi đó bước 2 tự bỏ qua và bước 1 bỏ qua model `size_vram = 0`.

**Đánh đổi.** `free_memory: true` xoá cache của ComfyUI nên job kế tiếp nạp lại
checkpoint (đọc đĩa/RAM). Chấp nhận: trên 4GB hai bên không cùng nằm trong VRAM được,
còn tràn VRAM thì chậm hơn nhiều hoặc OOM.

**Đã loại.**

- **Chỉ `num_gpu: 0` mặc định.** Đơn giản nhất nhưng đổi hẳn tốc độ chat/vision trên
  mọi máy; để thành tuỳ chọn cho tới khi đo thật trên RTX 3050.
- **Unload trong từng chỗ gọi ComfyUI.** Có 8 chỗ gọi; móc ở `run_comfy_job()` là đủ.

**Còn nợ.** Chưa đo thời gian nạp lại model và VRAM thực tế trên RTX 3050 — cần một lượt
chạy thật rồi ghi vào TESTLOG.

---

## D17 — Cho phép video ngắn từ ảnh persona (thay một phần D9)
**Ngày:** 2026-10-21 · **Trạng thái:** đã chốt · **Thay thế một phần D9**

**Bối cảnh.** D9 loại image-to-video với bốn lý do. Hai lý do đầu là về năng lực máy và
đã cũ:

1. "Không có chút năng lực video nào" — từ 2026-09-11 `ComfyUI/models` đã có bộ Wan 2.2
   TI2V 5B, đã kiểm SHA-256, node và model discovery (TESTLOG), dù **chưa chạy sinh video
   lần nào**.
2. "iGPU 4GB không chạy nổi" — máy hiện tại là RTX 3050 Laptop 4GB VRAM rời + ~32GB RAM
   (D16), không còn là Radeon 680M. 4GB vẫn ít; chưa có số đo nào cho biết chạy được tới đâu.

Hai lý do sau **vẫn đúng nguyên**: (3) một *tool* video sống bằng tốc độ, (4) tool phục vụ
nhiều người ⇒ cloud GPU + queue + billing, ngược D2. Cả hai chống mô hình *dịch vụ*, không
chống việc nhân vật thỉnh thoảng gửi một clip của chính mình — cùng loại với gửi ảnh ở trụ 3
(D1). Người dùng muốn có tính năng đó, và muốn cấu hình chạy được trên nhiều loại máy từ yếu
đến mạnh (STATE, mục Wan 2.2).

**Quyết định.**

1. **Cho phép image-to-video ngắn, đầu vào chỉ là ảnh của chính persona** (ảnh YumeAI đã tạo
   cho nhân vật đó). Đây là **tính năng phụ** của trụ 3 — "nhân vật gửi clip của mình" —
   không phải trụ mới, không phải sản phẩm.
2. **Chỉ bật khi hồ sơ máy cho phép.** Mặc định tắt; máy không đủ VRAM/RAM/đĩa thì tính năng
   không hiện ra, thay vì cho chạy rồi tràn VRAM hoặc treo máy. Ngưỡng của hồ sơ máy chỉ đặt
   từ số đo thật.
3. **Chỉ REST + web UI `/app` dùng lớp mới.** `app_gradio.py` giữ nguyên.
4. **Model mới đặt trong `project/ComfyUI/models`** (`COMFY_MODELS_DIR`), cùng chỗ model hiện
   có — không thêm thư mục model thứ hai.
5. **Làm theo giai đoạn**, giai đoạn sau chỉ bắt đầu khi giai đoạn trước có số đo. Giai đoạn 0
   chỉ đo nền trên máy thật bằng `project/bench_baseline.py`: text2img SD1.5 cold/warm, VRAM
   đỉnh, giá nhả VRAM Ollama (D16), chỗ trống đĩa (≥ 30 GiB cho model giai đoạn sau). Chưa
   thêm model hay tính năng nào.

**Giữ nguyên từ D9.** Không làm dịch vụ kiểu MotionMuse: không credit, không billing, không
cloud GPU, không hàng đợi nhiều người dùng, không phục vụ người ngoài. Giới hạn đầu vào ở ảnh
persona cũng là để dự án không trôi về tool stateless "vào ảnh bất kỳ → ra video" mà D1/D9
đã cảnh báo.

**Hệ quả.**

- D1 vẫn đứng trên: video là phụ, không chen trước ba trụ.
- Mọi job video đi qua `runtime.run_comfy_job()` (Resource Guard + D16) như mọi job ComfyUI.
- Logic pipeline mới nằm trong `yumeai/` (D6); `main.py` chỉ nhận input và đổi kiểu lỗi.
- Đầu vào chỉ là ảnh persona nên nguyên tắc consent ở `PRODUCT_ROADMAP.md` P3 (không nhận ảnh
  người thật) áp cho video y như ảnh.
- Kết quả từng giai đoạn ghi vào `docs/TESTLOG.md`; ngưỡng hồ sơ máy lấy từ đó.

**Đã loại.**

- **Giữ D9 nguyên văn.** Hai trong bốn lý do dựa trên phần cứng đã không còn; giữ thì lần nào
  nhắc tới video cũng phải cãi lại từ đầu.
- **Bật video cho mọi máy.** GPU 4GB có thể không chạy nổi model video nào ở chất lượng dùng
  được; không chặn theo hồ sơ máy thì người dùng gặp OOM/treo thay vì một tính năng tắt có lý do.
- **Video từ ảnh bất kỳ người dùng tải lên.** Đó chính là tool image-to-video D9 loại, và mở
  đường cho ảnh người thật.
- **Thêm video vào Gradio.** Gradio là cửa debug của khối ~3800 dòng đang cần tách (STATE, việc
  "Tách pipeline ảnh khỏi Gradio"); thêm vào là tăng nợ đúng chỗ đó.

**Còn nợ.**

- Chưa định nghĩa hồ sơ máy (trường nào, ngưỡng bao nhiêu) — chờ số đo giai đoạn 0. Hàm dò
  GPU/RAM/đĩa hiện nằm trong `bench_baseline.py`; khi làm hồ sơ máy thì chuyển vào `yumeai/`,
  không copy sang chỗ thứ hai.
- Chưa chạy sinh video lần nào trên RTX 3050 (TESTLOG 2026-09-11: "Chưa thử").

## D18 — Đổi tên nhân vật: Yuki thành Pao, Hana thành Su
**Ngày:** 2026-10-22 · **Trạng thái:** đã chốt

**Bối cảnh.** Người dùng đổi tên hai nhân vật — Yuki thành Pao, Hana thành Su — và muốn đổi
ở mọi chỗ: cả tên hiển thị lẫn id nội bộ, kể cả dữ liệu đã lưu. Bản thiết kế giao diện `/app`
mới đã dùng tên mới.

**Quyết định.**

1. **Đổi cả tên lẫn id:** `"yuki"` → `"pao"`, `"hana"` → `"su"` trong `personas.py`, `main.py`,
   `app_gradio.py`, `chat-ui-mockup.html`, test và tài liệu đang dùng. **Chỉ đổi tên**: tuổi, vai, tính cách,
   cách xưng hô (Pao tao/mày ↔ tớ/cậu, Su em/anh), few-shot và tag ngoại hình giữ nguyên.
2. **Chuyển dữ liệu cũ, không bỏ.** `YumeDatabase.initialize(personas, PERSONA_ID_ALIASES)`
   gọi `rename_characters()`: với mỗi nhân vật, tạo dòng mới → chuyển `conversations`,
   `memory_facts`, `images` → xoá dòng cũ. Mọi nhân vật đi chung **một** transaction và
   **một** bản sao lưu (backup API của sqlite, vào
   `data/backups/truoc-doi-ten-yuki-thanh-pao-hana-thanh-su-*.db`) — lỗi giữa chừng thì không
   ai bị đổi dở. Không còn id cũ thì không làm gì, nên mở app nhiều lần vô hại.
3. **Chỉ tiêu đề tự sinh được đổi** ("Trò chuyện với Yuki" → "Trò chuyện với Pao", tương tự
   Hana → Su). Tiêu đề người dùng tự đặt, nội dung tin nhắn và tóm tắt trí nhớ đã lưu giữ
   nguyên chữ.
4. **API vẫn nhận id cũ.** `resolve_persona_id()` đổi `"yuki"` → `"pao"`, `"hana"` → `"su"` ở
   mọi request (body lẫn query string), để tab `/app` mở từ trước hay script/curl cũ không bị 404.
5. `PERSONA_ID_ALIASES` **chỉ thêm, không xoá**: DB copy từ máy khác sang vẫn có thể còn id cũ.

**Hệ quả.**

- Trùng khoá trí nhớ (cùng user, cùng `fact_key` ở cả hai id) thì giữ bản của id mới.
- Tin nhắn cũ và `memory_summary` có thể vẫn còn chữ "Yuki"/"Hana" — đó là lời đã nói, không sửa.
- Các mục CHANGELOG/DECISIONS trước ngày này vẫn ghi Yuki/Hana vì đó là lịch sử.

**Đã loại.**

- **Chỉ đổi tên hiển thị, giữ id cũ.** Không đụng dữ liệu, nhưng code/DB gọi một tên còn
  giao diện gọi tên khác — người sau đọc code dễ nhầm. Người dùng chọn đổi hết.
- **`UPDATE characters SET id` tại chỗ.** Khoá ngoại không có `ON UPDATE CASCADE` và
  `foreign_keys` đang bật nên bị chặn; tắt `foreign_keys` để làm thì dễ để lại dòng mồ côi.
- **Thay tên cũ trong tin nhắn và tóm tắt.** Viết lại lời đã nói, và thay chuỗi mù trong
  văn bản tự do dễ sai nghĩa.

## D19 — Giao diện `/app` làm lại: "Ngân hà hồng"
**Ngày:** 2026-10-22 · **Trạng thái:** đã chốt

**Bối cảnh.** Giao diện `/app` cũ thô và rời rạc (một màn chat, thanh ảnh và trí nhớ dồn
một chỗ, hộp `prompt()`/`confirm()` của trình duyệt). Người dùng muốn một giao diện lãng mạn,
nền hồng tối dịu mắt để dùng trong phòng không bật đèn, và đã duyệt bản thiết kế trên canvas
trước khi viết code.

**Quyết định.**

1. **Một file HTML viết tay, không framework, không bước build** — `main.py` vẫn phục vụ
   `chat-ui-mockup.html` như file tĩnh, chạy offline trên máy người dùng.
2. **Bốn màn trong một trang**, đổi bằng `#hash` (nút Back vẫn dùng được): Trò chuyện, Tạo
   ảnh, Bộ sưu tập, Chọn nhân vật. Màn hẹp (≤ 900px) thì thanh bên và bảng hồ sơ thành ngăn kéo.
3. **Chỉ vẽ nút cho việc backend làm được.** Bản thiết kế có chọn kiểu video, khung hình, bộ
   vẽ và hàng chờ — REST chưa có nên `/app` chưa hiện; thêm khi lớp sinh ảnh mới (D17) có API.
   Nút bấm không chạy tệ hơn không có nút.
4. **Mặc định chất lượng là mức kỹ nhất** ("Tốt nhất", 50 bước): người dùng ưu tiên đẹp và
   chuẩn hơn nhanh. Giá trị gửi lên phải khớp hằng trong `comfy_service` — `test_web_ui.py` giữ.
5. **Chữ từ máy chủ/LLM/người dùng vào DOM bằng `textContent`**; `innerHTML` chỉ cho bộ icon
   SVG hằng số. Ký ức sửa/xoá ngay trong bảng (hỏi lại trước khi xoá) thay cho hộp thoại trình duyệt.
6. **Màu chữ kiểm tương phản ≥ 4.5:1** trên mọi nền đang dùng; đổi màu thì kiểm lại.
7. **Xin ảnh ngay trong khung chat.** `/app` hỏi `POST /api/chat/intent` trước mỗi tin nhắn;
   endpoint gọi đúng `prompt_router.plan_request()` của Gradio, không viết bộ định tuyến thứ
   hai. Ra `text2img` thì gọi `/api/images/generate` kèm `chat_message` — câu xin ảnh và ảnh
   chỉ được lưu vào hội thoại khi vẽ xong, như `/api/chat` chỉ lưu khi đã có câu trả lời.
   REST chưa nhận ảnh đính kèm nên các task sửa ảnh quy về trò chuyện.

**Hệ quả.**

- `app_gradio.py` không đổi (D17).
- Ảnh chân dung trong hồ sơ và thẻ nhân vật lấy ảnh gần nhất của nhân vật đó trong bộ sưu
  tập; chưa có ảnh thì hiện chữ cái đầu.
- Tạo ảnh qua REST là một request chờ tới khi xong, nên màn Tạo ảnh chỉ hiện thời gian đã
  chạy, chưa có số bước thật.
- Mỗi tin nhắn thêm một request định tuyến. Rule-based trả gần như tức thì; chỉ câu có nhắc
  tới ảnh/hình mà không rõ ý mới gọi thêm planner LLM (tối đa 25 giây).
- Ảnh xin trong chat dùng mức chất lượng và phóng to đang chọn ở màn Tạo ảnh (mặc định Tốt nhất).

**Đã loại.**

- **React/Vite.** Thêm bước build và `node_modules` cho một màn hình chạy local — thứ dễ hỏng
  nhất khi chép dự án sang máy khác.
- **Giữ nút cho tính năng chưa có, để mờ.** Người dùng bấm thử rồi không hiểu vì sao không
  chạy; ghi lại ở đây là đủ để nhớ sẽ thêm.

## D20 — Người dùng tự xoá và xuất được dữ liệu của mình
**Ngày:** 2026-10-22 · **Trạng thái:** đã chốt

**Bối cảnh.** Lộ trình P3 đòi trước khi gọi là beta: nói rõ đây là nhân vật AI, và người dùng
xoá/xuất được toàn bộ dữ liệu. Trước nay chỉ ký ức là xoá được; hội thoại, ảnh, cả DB thì
phải mở SQLite bằng tay.

**Quyết định.**

1. **Màn chào lần đầu** ở `/app` nói rõ Pao/Su là nhân vật AI do mô hình chạy trên máy viết
   ra, có thể nhớ sai/nói sai, và dữ liệu nằm ở `project/data`. Nhân vật vẫn không phá vai
   trong lúc chat (`personas.py`) — minh bạch nằm ở tầng sản phẩm, không ở lời nhân vật.
   Không làm age gate vì không có nội dung 18+.
2. **Xoá một ảnh** (`DELETE /api/gallery/{id}`) xoá dòng DB, file PNG và tin nhắn ảnh trỏ tới
   nó — để lại tin nhắn thì hội thoại hiện một khung ảnh hỏng.
3. **Xoá một hội thoại** (`DELETE /api/conversations/{id}`) xoá tin nhắn nhưng giữ ảnh trong
   bộ sưu tập (D14: ảnh thuộc về người dùng) và giữ ký ức (ký ức theo cặp người dùng–nhân vật,
   không theo hội thoại). Hộp xác nhận nói rõ cả hai điều đó.
4. **Xuất toàn bộ** (`GET /api/data/export`) là một zip: `data.json` + ảnh, đọc được không cần
   YumeAI.
5. **Xoá toàn bộ** (`POST /api/data/delete`) phải gõ `XOÁ`, xoá thật: không tự sao lưu, chạy
   `VACUUM` để chữ đã xoá không nằm lại trong trang trống của file DB. Nhân vật giữ nguyên.
   Bản sao lưu cũ trong `data/backups/` không bị đụng tới — giao diện nói rõ để người dùng tự xoá.
6. Ảnh cũ không rõ chủ (`user_id = ''`, trước migration v3) chỉ tính là của người dùng khi DB
   chỉ có một người dùng — cùng quy tắc migration đã dùng để gán chủ.

**Đã loại.**

- **Tự sao lưu trước khi "xoá toàn bộ".** Người dùng chủ động xoá thì một bản sao giấu đi
  là trái ý họ. Muốn giữ thì đã có nút xuất ngay phía trên.
- **Xoá hội thoại kéo theo ảnh.** Ảnh mất nhiều phút để vẽ, và gallery là nơi người dùng giữ
  ảnh; xoá ảnh là một thao tác riêng.

## D21 — Sao lưu / khôi phục bằng một file zip, từ chối khi app đang chạy
**Ngày:** 2026-10-22 · **Trạng thái:** đã chốt

**Bối cảnh.** Lộ trình P3.4 cần backup/restore DB + ảnh. Dữ liệu nằm ở hai chỗ (`yumeai.db`
chạy WAL và thư mục ảnh), nên copy tay dễ ra một bản thiếu: copy riêng file `.db` có thể thiếu
những trang còn trong `-wal`, copy DB mà quên ảnh thì gallery toàn khung hỏng.

**Quyết định.**

1. `project/sao_luu.py`, chỉ thư viện chuẩn, gói **một zip**: `yumeai.db` chụp bằng backup API
   của sqlite + `images/` + `manifest.json` (thời điểm, schema, số hội thoại/tin nhắn/ký ức/ảnh).
   Ghi vào `.zip.part` rồi mới đổi tên — mất điện giữa chừng không để lại bản sao lưu dở.
2. **Khôi phục từ chối khi cổng 8000/7860 đang có tiến trình** (main.py / app_gradio.py): thay DB
   dưới chân tiến trình đang mở nó là hỏng dữ liệu.
3. **Kiểm trước khi đụng:** có `yumeai.db`, `integrity_check = ok`, schema không mới hơn code.
4. **Luôn sao lưu dữ liệu hiện tại trước khi thay** (`truoc-khoi-phuc-*.zip`), và xoá `-wal`/`-shm`
   cũ để SQLite không áp nhật ký của DB cũ lên DB mới. Entry zip chỉ lấy tên file — không ghi ra
   ngoài thư mục ảnh.

**Đã loại.**

- **Nút khôi phục trong `/app`.** Khôi phục phải chạy khi app đã tắt, nên không thể là một nút
  của chính app đó.
- **Tự sao lưu theo lịch.** Chưa có cơ chế chạy nền khi app tắt; để người dùng chủ động chạy,
  thêm lịch sau nếu cần (Task Scheduler của Windows gọi được `python sao_luu.py`).

## D22 — Đo hiệu năng ngay lúc dùng thật: chỉ số, nằm trên máy
**Ngày:** 2026-10-22 · **Trạng thái:** đã chốt

**Bối cảnh.** Lộ trình P3.5 cần token/giây, thời gian nạp model, thời gian từng job ảnh.
`bench_baseline.py` (D17) đo một lần trong điều kiện dựng sẵn; nó không cho biết lúc dùng thật
— lịch sử dài, VRAM vừa nhường cho ComfyUI — máy chạy ra sao. Chọn model chat (STATE việc 5)
và báo trước cho người dùng một ảnh "Tốt nhất" mất bao lâu đều cần số thật của đúng máy này.

**Quyết định.**

1. `yumeai/core/perf.py`: mỗi lượt `/api/chat` và mỗi ảnh `/api/images/generate` nối **một dòng
   JSON** vào `<YUMEAI_DATA_DIR>/perf.jsonl`.
   - Chat: số từ chunk cuối của Ollama (eval, prompt_eval, load — nano giây đổi ra giây,
     token/giây) + thời gian dựng ngữ cảnh, chờ chữ đầu, cả lượt, tính từ lúc server nhận tin.
     Trạng thái `ok` / `empty` / `error` / `incomplete` / `cancelled`.
   - Ảnh: dịch/viết prompt, ComfyUI (gồm bước nhường VRAM D16), cả lượt, preset, model, kích
     thước. Trạng thái `ok` / `error` / `busy`.
2. **Chỉ số, không nội dung:** không tin nhắn, prompt, tên hay user_id; lỗi chỉ ghi tên lớp lỗi.
   Mỗi dòng có giờ đo, nên "Xoá toàn bộ dữ liệu" (D20) xoá luôn sổ. Sổ không vào file xuất
   (D20) và không vào bản sao lưu (D21) — đo lại được, không đáng giữ.
3. **Best-effort:** ghi lỗi chỉ cảnh báo một lần, không bao giờ làm hỏng lượt chat hay ảnh.
   Tự xoay vòng ở ~2 MB, giữ đúng một bản `.1`. Tắt bằng `YUMEAI_PERF_LOG=0`.
4. `GET /api/stats` trả trung vị/min/max trên 500 lần đo gần nhất, theo model chat và theo cặp
   chất lượng + phóng to; chỉ lần `ok` mới vào thời gian. `/app` dùng nó để ghi "thường mất
   khoảng…" ở màn Tạo ảnh và cạnh đồng hồ lúc vẽ.
5. Chỉ `main.py` ghi; Gradio giữ nguyên (D19).

**Đã loại.**

- **Bảng trong SQLite.** Số đo không phải dữ liệu sản phẩm: không cần migration, không nên dính
  vào xuất/sao lưu. JSON Lines đọc được bằng mắt và bằng bất kỳ công cụ nào.
- **Gửi số đo ra khỏi máy.** Trái với lời màn chào: "YumeAI không gửi chúng đi đâu".
- **Đo trong Gradio.** Khi pipeline ảnh tách sang `yumeai/pipelines/` (STATE việc 3) thì đo ở đó
  cho cả hai cửa vào, thay vì chép logic đo vào `app_gradio.py`.
- **Thanh tiến độ theo từng bước của ComfyUI** (websocket). Đáng làm nhưng là việc riêng; con số
  "thường khoảng" đã cho người dùng một mốc để chờ.

## D23 — Mở app bằng một cú bấm; Ollama nằm trên ổ rời, cổng riêng
**Ngày:** 2026-10-26 · **Trạng thái:** đã chốt

**Bối cảnh.** Mở app cần ba tiến trình trong ba cửa sổ (Ollama, ComfyUI, `main.py`) rồi tự mở
trình duyệt. Dự án nằm trên ổ rời mang qua nhiều máy, nhưng Ollama và model của nó lại cài trên
ổ C từng máy: máy mới là phải cài Ollama và `ollama pull` lại vài GB. Trên cùng ổ rời, PaoDich
đã chạy Ollama bản zip (không cần cài) ở cổng mặc định 11434, với thư mục model riêng.

**Quyết định.**

1. `project/khoi_dong.py` + `MO-PAOCAKE.cmd` (gốc dự án): bật những gì còn thiếu, chạy `main.py`
   (hoặc `--gradio`) ngay trong cửa sổ đó, mở `/app` khi `/health` trả lời. Chỉ thư viện chuẩn
   (+ `bench_baseline.query_gpus` để đọc VRAM); dùng chính Python của `.venv` cho ComfyUI.
2. Chọn Ollama theo thứ tự: `OLLAMA_URL` người dùng tự đặt → Ollama trên ổ rời
   (`<ổ>\Ollama\ollama.exe`, model ở `<ổ>\Ollama\models`, cổng **11435**; chưa chạy thì bật ẩn)
   → Ollama cài trên máy ở 11434 nếu đang chạy. Báo rõ khi thiếu Ollama hoặc thiếu đúng model
   đang cấu hình, nhưng vẫn mở app.
3. ComfyUI: GPU < 10 GB VRAM hoặc không đọc được → `--lowvram` (như trước); ≥ 12 GB → bỏ
   `--lowvram` và `YUMEAI_LOW_VRAM_MODE=0` nếu người dùng chưa tự đặt. Máy mạnh không bị kẹt ở
   chế độ tiết kiệm của máy 4 GB.
4. Chỉ tắt cái chính nó bật (nhả model Ollama trước để trả VRAM), cả khi bấm X đóng cửa sổ;
   cái đang chạy sẵn từ trước thì để nguyên.
5. Log của Ollama vào thư mục tạm của Windows, không vào `data/`: log có giờ từng lượt chat, để
   trong `data/` thì "Xoá toàn bộ dữ liệu" (D20) phải lo thêm nó.

**Đã loại.**

- **Dùng chung Ollama của PaoDich ở 11434.** Server nào bật trước thì thắng: app này sẽ không
  thấy model của mình, PaoDich thì đi tải lại model dịch. Cổng riêng tránh được cả Ollama cài
  sẵn trên máy.
- **Cài Ollama vào từng máy.** Ngược với cách làm việc trên ổ rời; mỗi máy lại tải model.
- **Để `main.py` tự bật Ollama/ComfyUI.** `main.py` là server; quản lý tiến trình con trong đó
  làm khó tắt êm, và Gradio sẽ phải làm lại y hệt.

## D24 — Model chat mặc định: qwen3:4b-instruct
**Ngày:** 2026-10-26 · **Trạng thái:** đã chốt (chủ dự án đồng ý)

**Bối cảnh.** STATE việc 5 cần chọn model chat bằng số đo chứ không bằng tên. `bench_chat.py` chạy
trên laptop RTX 3050 4 GB (TESTLOG 26/10), cùng bộ thoại Pao/Su, 24 câu mỗi model.

**Quyết định.** `OLLAMA_MODEL` mặc định đổi từ `qwen2.5:3b` sang `qwen3:4b-instruct`
(`yumeai/core/config.py`, env.example, RUNBOOK; `khoi_dong.py` đọc đúng giá trị đó).

| | qwen2.5:3b | qwen3:4b-instruct |
|---|---|---|
| Giữ vai (đạt mọi luật) | 17/24 | 23/24 |
| Nhớ đúng thông tin trong trí nhớ | 0/4 | 3/4 |
| Token/giây · cả câu | 64 · ~1 s | 15 · ~4 s |
| Bộ nhớ | 2.0 GiB, 100% GPU | 3.3 GiB, 67% GPU |

Đọc câu trả lời: qwen2.5:3b lẫn cả câu tiếng Trung, bịa thông tin người dùng, gạch đầu dòng khi bị
dụ, có câu thô tục. Sản phẩm là "trò chuyện có trí nhớ" (AGENTS mục 1) và chủ dự án ưu tiên đúng
hơn nhanh, nên chọn model giữ vai và nhớ tốt hơn, chấp nhận chậm hơn trên máy 4 GB.

**Hệ quả cần theo dõi.** Dịch VI→EN và tóm tắt trí nhớ vẫn tự dò, ưu tiên `qwen2.5:3b`
(`get_translate_model()`), nên trên GPU 4 GB Ollama có thể phải đổi qua lại hai model (nạp lại
~4–6 giây). `/api/stats` (D22) đếm số lần nạp lại (`reloads`) theo model: nhiều thì đặt
`YUMEAI_TRANSLATE_MODEL=qwen3:4b-instruct` để chỉ dùng một model — chưa làm vì chất lượng dịch prompt
ảnh của qwen3 chưa được đo.

**Đã loại.**

- **Giữ qwen2.5:3b vì nhanh.** Nhanh mà tuột vai và quên tên người dùng thì hỏng đúng trụ sản phẩm.
- **Chọn model theo VRAM của máy.** Chưa có số đo trên máy GPU lớn; chọn một mặc định, máy nào cần
  nhanh thì `MO-PAOCAKE.cmd --model qwen2.5:3b` hoặc `OLLAMA_MODEL`.

## D25 — Trí nhớ chỉ ghi điều người dùng tự nói
**Ngày:** 2026-10-27 · **Trạng thái:** đã làm, chờ chạy thử lại trên máy

**Bối cảnh.** Dùng thật 27/10 (TESTLOG): với Pao, 7/13 fact tự trích là chuyện của **nhân vật** ghi
như của người dùng (`interest: vẽ mưa đêm và mèo hoang`, `honorific: Pao`…), có cả fact sai có hại
`boundary: không muốn nhắc đến việc nuôi chó hoặc mèo` trong khi người dùng nuôi chó. Tóm tắt hội
thoại cũng ghi "Tên: Pao". Nguyên nhân: bản ghi gửi cho model chỉ có `user:`/`assistant:`, model
không biết "assistant" là Pao; khoá fact tự do nên model bịa khoá mới mỗi lần.

**Quyết định** (`yumeai/services/memory_service.py`, `ollama_service.summarize_conversation`):
- Bản ghi gửi đi gọi rõ hai người: `NGƯỜI DÙNG` và `<tên> (nhân vật)`; prompt trích fact và prompt
  tóm tắt nói thẳng đó là hai người khác nhau, không ghi chuyện của nhân vật.
- Bộ khoá **cố định** `FACT_KEYS` (13 khoá, kèm nhãn tiếng Việt). Khoá quen thuộc ngoài bộ được quy
  về khoá chuẩn (`hobby` → `likes`, `location` → `home_city`…); khoá lạ bị bỏ.
- Mỗi fact phải có `evidence` — nguyên văn câu người dùng — và phải **đối chiếu được với một câu
  khẳng định của người dùng**: giá trị trùng ≥ 50% từ có nghĩa, bằng chứng trùng ≥ 60%. Câu hỏi và
  câu nhờ vả ("kể đi", "vẽ cho anh…") không tính là lời khẳng định. Giá trị chứa tên nhân vật bị bỏ.
- Tóm tắt bỏ dòng rỗng kiểu "Tuổi: Chưa có".
- `/api/memory` trả thêm `labels` để bảng "… nhớ về bạn" hiện nhãn tiếng Việt.

Thử trên đúng dữ liệu 27/10: cả 7 fact sai bị bỏ, các fact đúng giữ nguyên.

**Đã loại.**
- **Chỉ sửa prompt.** Model 3–4B vẫn lẫn dù được dặn; cần lớp kiểm bằng code không phụ thuộc model.
- **Gộp mọi câu người dùng rồi so.** Đã thử: quá lỏng (7 fact sai vẫn lọt) vì từ rải rác khắp hội
  thoại ghép lại thành gần như mọi thứ. Phải khớp trong **một** câu.
- **Bỏ hẳn tự trích fact.** Mất trụ "trò chuyện có trí nhớ"; người dùng vẫn sửa/xoá được trong bảng.

## D26 — Câu hỏi có chữ "vẽ" là trò chuyện, không phải lệnh vẽ
**Ngày:** 2026-10-27 · **Trạng thái:** đã làm

**Bối cảnh.** "Pao đang vẽ gì vậy?" bị bộ định tuyến hiểu là xin ảnh và vẽ luôn (TESTLOG 27/10, lỗi 1).
Pao là sinh viên hội hoạ nên chữ "vẽ" xuất hiện trong trò chuyện thường xuyên.

**Quyết định** (`yumeai/core/prompt_router.py`, `looks_like_create`). Câu có "vẽ/draw" mà có dấu hiệu
câu hỏi (`?`, hoặc kết thúc bằng gì/không/chưa/sao/à/hả/nhỉ/vậy/thế nào/bao giờ/ở đâu…) thì là trò
chuyện — **trừ khi** có dấu hiệu nhờ vẽ rõ: "vẽ cho/giúp/thử/một/tấm/bức…", "vẽ anh/em/mình…",
kết bằng "được không/nhé/nha/đi", hoặc "can you/please/for me". Vì vậy "vẽ cho anh một tấm được không?"
vẫn là xin ảnh. `test_router.py` mục 3b: 7 câu trò chuyện, 6 câu xin ảnh.

**Đã loại.** **Hỏi LLM cho mọi câu có chữ "vẽ".** Thêm vài giây mỗi lượt và lại phải đổi model trên GPU 4 GB.

## D27 — Ảnh vẽ chính nhân vật: không tên trong prompt, bối cảnh quen thuộc, có câu kèm theo
**Ngày:** 2026-10-27 · **Trạng thái:** đã làm, chờ chạy thử lại trên máy

**Bối cảnh.** Dùng thật 27/10 (lỗi 6): prompt ảnh Pao có tag `1pao` và bối cảnh "Chinese ink art
studio" (Pao học ở Tokyo); ảnh Pao cầm cọ đưa lên miệng chứ không đang vẽ. Ảnh gửi trong hội thoại
không kèm câu nào của nhân vật — chú thích dưới ảnh lại là câu xin của người dùng. Cùng đợt: Pao lặp
"B-boys." ở 9/13 câu (bản méo của "B-baka" trong few-shot) và có câu tự xưng "Mình" (lỗi 5).

**Quyết định.**
- `personas.py`: thêm `scene` (nơi quen thuộc, tiếng Anh) và `image_lines` (câu nói khi gửi ảnh).
  Pao: few-shot "B-baka!" → "Baka!", lời chào bỏ "H-hừ"; speech_style chỉ cho chêm đúng Baka/Urusai/
  Mou, cấm tiếng Anh, từ bịa, nói lắp và xưng "mình"/"tôi".
- `yumeai/core/image_prompt.py`: `persona_neutral_text()` thay tên nhân vật bằng "cô ấy" trước khi
  dịch; `scrub_persona_name()` đổi `1<tên>` → `1girl` và bỏ tên khỏi prompt ra; `image_caption()` chọn
  câu kèm ảnh theo seed.
- `enrich_for_create_ex(..., persona=None)`: có nhân vật thì dặn thêm: một cô gái (`1girl, solo`),
  không dùng tên làm tag, không tả mặt/tóc (đã có `appearance`), hành động phải thấy rõ (vẽ = cọ chạm
  giấy), bối cảnh mặc định là `scene` nếu người dùng không nói nơi. **app_gradio.py không truyền
  `persona` nên prompt của Gradio giữ nguyên.**
- `/api/images/generate` có `chat_message` thì lưu câu nhân vật thành **lượt chữ riêng** ngay trước ảnh
  và trả `caption`. `/app` hiện câu đó thành bong bóng nhân vật; chú thích dưới ảnh ghi "Bạn xin: …".

**Đã loại.**
- **Lưu câu kèm ảnh bên trong JSON của lượt ảnh.** Lượt ảnh không đi vào lịch sử chat, nên model sẽ
  không biết nhân vật vừa nói gì; lượt chữ riêng thì có, và mở lại hội thoại hiện đúng mà không cần sửa UI.
- **Cho model chat viết câu kèm ảnh.** Thêm một lần gọi model và chờ thêm vài giây sau khi đã chờ
  vẽ 40–99 s; câu viết sẵn đúng giọng là đủ ở bước này.

## D28 — Sau lần chạy lại 28/10: lọc tóm tắt, emoji và tag ngoại hình
**Ngày:** 2026-10-28 · **Trạng thái:** đã làm, chờ chạy lại trên máy

**Bối cảnh.** Chạy lại đúng kịch bản 27/10 sau D25–D27 (TESTLOG 28/10). Trích fact đã đúng hẳn, nhưng:
- **Tóm tắt hội thoại** (qwen2.5:3b) vẫn viết toàn chuyện của Pao ("- Pao sống ở Đà Nẵng" — sai cả hai phía)
  dù prompt đã gọi đúng tên hai người. Tóm tắt được đưa vào prompt dưới mục "những gì đã biết về người
  này"; ngay sau đó Pao trả lời "Mình tên Minh…".
- Pao kết câu bằng emoji (✖️, 🚫) dù hồ sơ cấm.
- Model dịch vẫn tả tóc trong prompt ảnh ("long black hair tied up" cho Su tóc nâu nhạt) dù đã dặn không tả.

**Quyết định.**
- `memory_service.ground_summary()`: mỗi dòng tóm tắt phải qua đúng luật của fact (D25) — không chứa tên
  nhân vật, khớp một câu khẳng định của người dùng; không tính các từ chỉ chủ ngữ ("người dùng", "họ",
  "tên"). Áp cho cả bản vừa tính lẫn bản đã lưu trong DB (bản cũ có thể còn dòng sai). Trên dữ liệu thật:
  cả hai bản tóm tắt của lần chạy lại bị bỏ hết; bản trộn đúng/sai chỉ còn dòng về người dùng.
- `chat_context.strip_emoji()` lọc emoji ngay trên luồng token của `/api/chat` (người dùng không thấy,
  DB không lưu). Gradio không đổi.
- `image_prompt.drop_look_tags()`: khi vẽ chính nhân vật, bỏ tag tóc/mắt/mặt model tự thêm — ngoại hình
  lấy từ `appearance`.

**Đã loại.**
- **Sửa tiếp prompt tóm tắt.** D25 đã sửa prompt; model 3B vẫn sai. Lớp kiểm bằng code là chỗ chắc chắn.
- **Dặn thêm "không dùng emoji" trong prompt.** Hồ sơ đã dặn; lọc ở luồng thì chắc chắn và rẻ.

**Chưa quyết (chờ chủ dự án).** Bước tóm tắt chạy **đồng bộ** trước câu trả lời và đổi model trên GPU 4 GB:
lần chạy lại có một lượt chờ 58 s chỉ ở bước dựng ngữ cảnh, và cả hai bản tóm tắt đều bị bỏ hết sau khi lọc.
Fact (D25) đã mang đủ thông tin người dùng. Lựa chọn: (a) tắt tóm tắt mặc định (`YUMEAI_CHAT_SUMMARY=0`);
(b) tóm tắt bằng chính model chat (`YUMEAI_TRANSLATE_MODEL=qwen3:4b-instruct`, cần đo chất lượng dịch prompt
ảnh); (c) chuyển tóm tắt sang chạy nền như trích fact.

## D29 — Tắt tóm tắt hội thoại mặc định; fact ghi đủ, không trùng, có ngày
**Ngày:** 2026-10-28 · **Trạng thái:** đã làm (chủ dự án chọn phương án (a) trong D28 "Chưa quyết")

**Bối cảnh.** D28 để ngỏ bước tóm tắt hội thoại: chạy đồng bộ trước câu trả lời, đổi model trên GPU 4 GB
(một lượt chờ 58 s), và cả hai bản tóm tắt của lần chạy lại đều là chuyện của nhân vật nên bị lọc sạch.
Fact (D25) đã mang đủ thông tin về người dùng. Cái giá của việc tắt: chuyện cũ hơn cửa sổ 12 tin mà không
thành fact sẽ bị quên — đúng 28/10, "mai đi công tác Sài Gòn" không thành fact.

**Quyết định.**
- `YUMEAI_CHAT_SUMMARY` mặc định `0` (`config.py`, `env.example`, RUNBOOK). Tắt thì không gọi LLM tóm tắt,
  không dùng bản tóm tắt đã lưu trong DB, và chỉ đọc đúng cửa sổ gần (`history_load_limit`). Bật lại bằng
  `YUMEAI_CHAT_SUMMARY=1` — toàn bộ đường tóm tắt (D13, D28) vẫn giữ và vẫn có test. Gradio dùng chung cấu hình
  nên cũng tắt; `app_gradio.py` không đổi.
- Bù phần mất bằng fact: prompt trích fact giải thích `current_event` (việc đang hoặc sắp xảy ra, giữ nguyên mốc
  thời gian người dùng nói) và `promise`, có ví dụ. Khi đưa vào prompt chat, hai khoá này kèm ngày ghi và một
  dòng "Hôm nay là …" để nhân vật biết "mai" là mai của hôm nào.
- Chạy lại cho thấy lô đầu của Pao ghi con chó vào cả `family` lẫn `pet`, cà phê vào cả `likes` lẫn
  `favorite_food_drink`, mà bỏ sót `home_city` → Pao trả lời "ở Sài Gòn". Prompt dặn thêm: một câu có thể chứa
  nhiều điều, ghi đủ; mỗi điều một lần, vào khoá cụ thể nhất (ví dụ "Mình là Lan, giáo viên ở Huế"). Code bỏ
  fact ở khoá chung (`likes`, `family`) nếu trùng một fact cụ thể hơn trong cùng lô. Kiểm lại ngay trên máy:
  lô đầu 6/6 đúng, có "Nơi sống", không trùng (một lần chạy).

**Đã loại.**
- **(b) Tóm tắt bằng model chat:** vẫn bắt người dùng chờ (qwen3 ~15–19 token/giây, 15–20 s mỗi lần) và cùng
  biến đổi luôn model viết prompt ảnh, chưa đo.
- **(c) Chạy nền:** thêm code mà kết quả vẫn bị lọc gần hết, và vẫn tranh GPU với lượt chat kế tiếp (trích
  fact chạy nền đã làm một lượt chờ 18 s, xem TESTLOG 28/10 tối).
- **Gộp fact trùng trên DB** (khác lô): chưa làm; bộ lọc chỉ xét trong một lô.

## D30 — Tên người dùng lấy từ fact; thử cho chat "chen ngang" trích fact rồi bỏ
**Ngày:** 2026-10-29 · **Trạng thái:** đã làm (phần tên); phần chen ngang đã thử và loại

**Bối cảnh.** Đo kịch bản "Tuấn" nhiều lần bằng script chạy trong trang `/app` (TESTLOG 29/10), mỗi lần một
`user_id` riêng, cách nhau 6 s giữa hai câu. Hai việc lộ ra:
- **Tên giả "Minh":** `chat-ui-mockup.html` ghi cứng `USER_NAME = 'Minh'` và gửi kèm mỗi lượt chat, nên prompt
  luôn có "Tên người bạn đang nhắn tin: Minh". Khi lời tự giới thiệu đã trôi khỏi cửa sổ 12 tin mà chưa có fact,
  nhân vật đáp "Mình tên Minh…" (gặp 28/10 và 29/10).
- **Chờ sau lô trích fact:** Ollama làm tuần tự, lô trích fact chạy nền ~15–22 s, nên câu kế tiếp chờ 8–16 s
  (mọi lần chạy, ngay sau mỗi lô).

**Quyết định.**
- `memory_service.known_user_name()`: fact `name` (người dùng tự giới thiệu) thắng `user_name` gửi kèm và tên
  lưu trong bảng users. `/app` thôi gửi tên giả (`USER_NAME = ''`, thanh bên hiện "Bạn").
- `/app` trả `Cache-Control: no-cache`: không có nó, trình duyệt cứ chạy giao diện cũ sau khi cập nhật (gặp lúc
  đo: sửa xong, tab vẫn gửi "Minh").

**Đã thử và loại — cho lượt chat chen ngang lô trích fact.** Lô trích fact gửi dạng stream, có lượt chat thì
đóng kết nối (Ollama dừng sinh) và chạy lại sau.
- **Nhường vô hạn:** lượt chat hết chờ (≤ 1,1 s) nhưng người gõ đều tay thì lô không bao giờ chạy xong; tới câu
  hỏi trí nhớ chưa có fact nào → Pao đáp "Mình tên Minh, làm thiết kế đồ họa… Ở Sài Gòn", "Không nuôi con nào
  cả". Lô dồn lại thành 24 tin nên mất luôn chuyện công tác Sài Gòn.
- **Nhường một lần mỗi lô:** chỉ dời lượt chờ sang câu sau (câu 7 nhanh, câu 8 chờ 15–17 s); fact và trí nhớ
  không khá hơn (một lần thiếu "Nơi sống" → lại "ở Sài Gòn").
- Người gõ bình thường (15–30 s mỗi câu) thì lô xong trong lúc họ gõ, có nhường hay không như nhau. Thêm độ phức
  tạp và rủi ro làm đói trí nhớ mà không bớt được giây chờ nào → bỏ. Muốn bớt chờ thật thì phải làm lô trích fact
  **nhẹ hơn** (ít token ra hơn) — việc tiếp theo, cần đo.
