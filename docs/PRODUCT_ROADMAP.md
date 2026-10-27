# Lộ trình sản phẩm — YumeAI companion

**Cập nhật:** 2026-10-27

Mục tiêu là một AI companion local-first có nhân vật nhất quán, trò chuyện nhớ
người dùng và gửi lại ảnh của chính nhân vật đó. Đây không phải lộ trình cho một
công cụ sửa ảnh hoặc dịch vụ image-to-video.

## Kiến trúc đã chọn

```text
Web UI (/app) hoặc Gradio
          │
          ▼
FastAPI ──┬── SQLite: user, conversation, message, memory, gallery
          ├── Ollama: persona chat, summary, sau này fact extraction/embedding
          └── ComfyUI: tạo và sửa ảnh (Resource Guard: một job mỗi lần)
```

Giữ kiến trúc này vì nó chạy được trên máy yếu (chọn từ thời máy cũ Radeon 680M + khoảng 12 GiB RAM;
nay là laptop RTX 3050 4 GB — [`MAY.md`](MAY.md)), không có chi phí API và dùng lại toàn bộ
pipeline ảnh hiện có.

## Đã hoàn thành — nền móng sản phẩm

- Persona Pao/Su (trước đây tên Yuki/Hana — D18), few-shot, chat streaming và router chat/tạo ảnh.
- SQLite với schema `users`, `characters`, `conversations`, `messages`,
  `memory_facts`, `images`.
- REST API tạo/liệt kê/mở conversation; server tự lưu user và assistant message.
- Gallery lưu PNG cùng prompt, model, style, seed và metadata sinh ảnh.
- Web UI đọc conversation thật, khôi phục lịch sử sau khi đóng app, chọn persona
  từ sidebar và dùng image URL đã lưu.
- Loại bỏ mock response, Picsum, credit và affection ngẫu nhiên khỏi UI.
- Cache tóm tắt được tách theo fingerprint lịch sử, không rò trí nhớ giữa hai
  conversation có cùng persona.

## P1 — chatbot có trí nhớ đúng nghĩa — nền tảng đã hoàn thành

1. ✅ Fact extraction đã tách khỏi luồng trả lời: mặc định mỗi 12 text message,
   Ollama trích fact ổn định ở background với context 2K.
2. ✅ Upsert vào `memory_facts`, có nguồn message/confidence; UI cho xem, thêm,
   sửa, xoá và refresh trí nhớ.
3. ✅ Fact + summary được ghép vào system prompt. Hiện đưa tối đa 24 fact gần nhất;
   relevance retrieval bằng FTS5/embedding chỉ cần khi dữ liệu lớn hơn.
4. Khi có hàng nghìn fact mới thử Ollama `/api/embed` + cosine search hoặc
   sqlite-vec. Không đưa Mem0/Qdrant vào trước khi benchmark chứng minh cần thiết.

## P2 — nhất quán nhân vật và gallery

1. Mỗi persona có ảnh gốc chuẩn, seed mặc định và preset IP-Adapter/ReActor riêng.
2. Mọi entrypoint gọi chung pipeline trong `yumeai/pipelines/`; không để logic ảnh
   tiếp tục nằm trong khối Gradio 3.800 dòng.
3. ✅ (D19, 2026-10-22) Gallery dạng lưới trong `/app`: lọc theo nhân vật, xem chi tiết,
   vẽ lại với lời tả cũ. API `GET /api/gallery` lọc được theo user/hội thoại.
4. LoRA chỉ triển khai khi có GPU rời đủ VRAM và đã đo trên máy thật.

## P3 — từ app local sang bản beta an toàn

1. ✅ (D20) Màn chào lần đầu ở `/app` ghi rõ đây là nhân vật AI và dữ liệu nằm ở đâu.
   Age gate chỉ cần nếu bật nội dung 18+ — hiện không có.
2. Nếu chỉ bind `127.0.0.1`, `user_id` local là đủ. Nếu mở LAN/Internet phải thêm
   đăng nhập, hash mật khẩu, session, rate limit. CORS `*` đã bỏ (2026-10-15).
3. Chính sách consent: không nhận ảnh người thật khác nếu không có sự đồng ý; cấm
   nội dung về trẻ vị thành niên. ✅ (D20) Xoá ảnh, xoá hội thoại, xuất và xoá toàn bộ dữ liệu.
4. ✅ (D21) Sao lưu/khôi phục DB + ảnh bằng `sao_luu.py`; test migration v1→v2→v3 đã có.
   Còn thiếu: migration runner tuần tự (STATE, nợ kỹ thuật).
5. ✅ (D22) Số đo thật mỗi lượt chat/ảnh vào `data/perf.jsonl`, xem bằng `GET /api/stats`.

## P4 — tính năng tăng giá trị, không phải điều kiện MVP

- TTS/STT local và voice message.
- Notification/chủ động nhắn theo lịch local.
- Character card import/export và lorebook.
- Multi-device sync hoặc cloud GPU là một mode riêng, không làm mặc định.

## Các giải pháp đã cân nhắc

| Giải pháp | Dùng thế nào | Quyết định hiện tại |
|---|---|---|
| SillyTavern | Tham khảo character card, lorebook, chat UX | Không nhúng; khó nối sạch pipeline ComfyUI riêng |
| Mem0 self-hosted | Fact extraction + semantic memory | Hoãn; thêm vector DB/dịch vụ và tốn RAM quá sớm |
| SQLite + FTS5 | Lưu trữ và tìm text local | Chọn cho MVP |
| Ollama embeddings | Semantic retrieval khi dữ liệu lớn | Cổng mở cho P1/P2, chưa bật mặc định |

Tham khảo chính thức: [Ollama embeddings](https://docs.ollama.com/api/embed),
[SQLite FTS5](https://www.sqlite.org/fts5.html),
[SillyTavern World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/),
[Mem0 OSS](https://github.com/mem0ai/mem0).
