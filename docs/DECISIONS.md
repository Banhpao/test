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
**Ngày:** 2026-08-18 · **Trạng thái:** đã chốt — loại bỏ

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
