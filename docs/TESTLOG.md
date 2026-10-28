# Nhật ký test — YumeAI

Nơi ghi kết quả chạy thử **trên máy khác** (máy mạnh hơn, GPU rời, cấu hình khác).
Người/AI đọc file này để biết cái gì thực sự chạy được ở đâu, không phải đoán.

Mục mới nhất ở **trên cùng**.

> **Vì sao cần file này:** nhiều giới hạn trong code được đặt cho máy phát triển yếu —
> trước đây là iGPU AMD Radeon 680M (`DECISIONS.md` D3), nay là laptop RTX 3050 4 GB
> ([`MAY.md`](MAY.md) ghi máy đang dùng). Khi test trên máy khác, ghi lại đây để biết giới
> hạn nào là **thật** và giới hạn nào chỉ là **do máy yếu**.

---

## 2026-10-28 — NVIDIA GeForce RTX 3050 Laptop GPU 4 GB, RAM 31.7 GiB — chạy lại kịch bản 27/10 sau khi sửa 6 lỗi (D25–D27)

**Cấu hình chạy**
- Code ở commit `7446196`. Như 27/10: `qwen3:4b-instruct`, dịch/tóm tắt `qwen2.5:3b`, ComfyUI `--lowvram`, "Tốt nhất (50 steps)".
- Mở bằng `_vscode\MO-PAOCAKE-DU-LIEU-THU.cmd`: dữ liệu riêng `project\data\thu-lai\20261028-083706\` (DB trống như 27/10);
  DB thật không bị đụng.
- Đúng 14 câu Pao và 7 câu Su của 27/10 (lấy từ DB), gửi qua chính hàm `send()` của `/app`; thêm 1 câu xin ảnh Pao
  ("Vẽ cho mình một tấm Pao đang ngồi ký hoạ đi.") để kiểm lỗi 6. Máy mất kết nối ~9 giờ giữa câu 10 và 11 của Pao
  (laptop ngủ); app vẫn chạy, câu 10 gửi lại.

| Hạng mục | 27/10 | 28/10 |
|---|---|---|
| Trích fact Pao | 6/13 đúng, thiếu con chó | **8/8 đúng**, có con chó Bơ (2 cặp gần trùng: `family`/`pet`, `likes`/`favorite_food_drink`) |
| Trích fact Su | 5/7 | **6/6 đúng** |
| Tóm tắt hội thoại Pao | "Tên: Pao", "Pao - B-boys" | vẫn sai: 2 bản, cả 9 dòng là chuyện của Pao ("Pao sống ở Đà Nẵng") → D28 |
| "Pao đang vẽ gì vậy?" | vẽ ảnh luôn | trò chuyện (lỗi 2 hết) |
| Enter lúc đang bận | không gửi, không báo | dòng gợi ý "Pao đang trả lời — đợi xong rồi nhấn Enter…", nháy; chữ giữ nguyên (lỗi 3 hết) |
| Bảng "… nhớ về bạn" | không tự làm mới, khoá tiếng Anh | Su: hiện 6 fact sau ~40 s không cần tải lại; nhãn tiếng Việt (lỗi 4 hết) |
| "B-boys" / xưng "Mình" | 9/13 câu / có | 0 / 2 câu (lặp "mình" của người dùng khi trả lời câu hỏi trí nhớ) |
| Prompt ảnh | `1pao`, "Chinese ink art studio" | `1girl, solo`, xưởng vẽ / phòng gỗ Kyoto đúng `scene`; còn tả tóc trái ngoại hình → D28 |
| Câu kèm ảnh | không có | Pao "Hừ, cầm lấy…", Su "Anh xem nhé…" (lỗi 6 hết) |
| Ảnh Su đang viết | không thấy viết | **đang cầm bút viết vào sổ**, áo cardigan kem, bên cửa sổ |
| Ảnh Pao đang ký hoạ | cầm cọ đưa lên miệng | ngồi cạnh giá vẽ, chưa thấy rõ đang vẽ |
| Nhớ trong hội thoại (Pao) | tên, nghề, nơi đúng; đồ uống, chó "không biết" | đồ uống và chó đúng; **tên sai ("Minh")** ngay sau bản tóm tắt sai |

**Số đo** (`/api/stats`, `perf.jsonl` của thư mục thử): chat 20 ok + 1 huỷ (lúc mất kết nối); cả lượt trung vị 3,7 s;
chữ đầu trung vị 0,49 s; 19,2 token/giây; `reloads` 4/21. Ảnh 59 và 91 s (ComfyUI 52 và 84 s, viết prompt ~5 s).
Lượt chậm: khởi động lạnh 31 s; câu 7 chờ chữ đầu 11 s (không nạp model — có lẽ đang xếp hàng sau lượt trích fact
chạy nền); **câu 13 chờ 58 s ở bước dựng ngữ cảnh** (tóm tắt bằng qwen2.5:3b, đổi model trên GPU) rồi 4,6 s nạp lại.

**Lỗi còn lại**
1. Tóm tắt hội thoại vẫn lẫn nhân vật với người dùng, lại tốn 58 s → đã lọc bằng code (D28); nên quyết có giữ bước này.
2. Pao chèn emoji (✖️, 🚫) ở 2 câu → lọc trên luồng (D28).
3. Pao trả lời câu dụ AI: "Không ai biết tao là AI cả…" — không nhận thẳng nhưng hở. Su lần này **liệt kê gạch đầu dòng**
   ba câu trích (27/10 không). Mỗi kịch bản chỉ chạy một lần nên chưa nói được là tệ hơn; cần `bench_chat.py` nhiều lượt.
4. Prompt ảnh vẫn tả tóc trái ngoại hình (Su "long black hair") → bỏ tag tóc/mắt/mặt (D28). Ảnh Pao chưa rõ hành động vẽ.

---

## 2026-10-27 — NVIDIA GeForce RTX 3050 Laptop GPU 4 GB, RAM 31.7 GiB — lần đầu dùng thật `/app` (chat + vẽ ảnh)

**Phần cứng**
- Như mục 26/10 bên dưới ([`MAY.md`](MAY.md)).

**Cấu hình chạy**
- Mở bằng `MO-PAOCAKE.cmd`: Ollama trên ổ rời cổng 11435, chat `qwen3:4b-instruct` (D24), dịch/tóm tắt tự dò ra `qwen2.5:3b`;
  ComfyUI `--lowvram`; checkpoint `anythingAnd_anythingAndEverything.safetensors`; ảnh mức "Tốt nhất (50 steps)", không upscale, 512x768.
- Không đổi biến môi trường. Dữ liệu trước khi thử (DB gần như trống) sao lưu ở
  `E:\duan\_backup_claude\paocake-data-truoc-thu-app-2026-10-27.zip`.
- Gõ qua `/app` trong trình duyệt tích hợp của Claude; khung hẹp nên ban đầu là bố cục điện thoại.
- Người dùng giả "Tuấn": kỹ sư điện tử ở Đà Nẵng, thích cà phê sữa đá, nuôi chó tên Bơ. Pao 14 lượt, Su 7 lượt;
  có câu dụ nhận AI, dụ liệt kê, hỏi tiếng Anh, hỏi lại trí nhớ; 2 ảnh xin trong khung chat.

**Kết quả** (số từ `GET /api/stats`; chat gồm cả 1 lượt ngày 26/10)
| Hạng mục | Kết quả | Thời gian | Ghi chú |
|---|---|---|---|
| Chat `qwen3:4b-instruct` | 20/20 ok | cả lượt trung vị 4,3 s (3,4–34,5); chữ đầu trung vị 0,9 s; 14,5 token/giây | `reloads` = 5, xem Kết luận |
| text2img SD1.5 qua `/app` (2 ảnh) | 2/2 ok — **lần đầu vẽ thật trên máy này** | ComfyUI 34–72 s; cả lượt 40–99 s; dịch prompt 4,6–26 s | ảnh ở `data/images/`, có trong bộ sưu tập |
| Giữ vai khi bị dụ (AI / liệt kê / tiếng Anh) | Pao 3/3, Su 2/2 | | Su chưa thử tiếng Anh |
| Nhớ trong cùng hội thoại | Pao: tên, nghề, nơi ở đúng; đồ uống và con chó "không biết". Su: đủ tên, đồ uống, con chó | | |
| Trích fact (`memory_facts`) | Pao: 6/13 đúng về người dùng, 7 sai; **thiếu con chó Bơ**. Su: 5/7 đúng, 2 rác | chạy ngầm sau mỗi 12 tin | xem lỗi 4 |

**Lỗi gặp phải**
1. **Bộ định tuyến:** câu hỏi "Pao đang vẽ gì vậy?" bị hiểu là lệnh vẽ ảnh và vẽ luôn. Có chữ "vẽ" là đủ, kể cả câu hỏi.
2. **Đang vẽ ảnh thì Enter không gửi và không báo gì.** Chữ gõ thêm nằm lại trong ô; tin sau còn bị chèn vào giữa tin trước.
3. **Bảng "… nhớ về bạn" không tự làm mới** khi fact được trích ngầm (Su có 7 fact trong DB, bảng vẫn ghi "chưa ghi nhớ
   điều gì" tới khi tải lại trang). Nhãn hiện thẳng khoá tiếng Anh (`memory commitment`, `pet`, `relationship`).
4. **Trích fact lẫn nhân vật với người dùng** (Pao): lưu cả chuyện của Pao như `interest: vẽ mưa đêm và mèo hoang`,
   `goal: hoàn thành bài vẽ deadline`, `event: mưa to tại Tokyo`; `honorific: Pao`; và một fact sai có hại
   `boundary: không muốn nhắc đến việc nuôi chó hoặc mèo` — trong khi người dùng nuôi chó. Tóm tắt hội thoại cũng lẫn:
   "Tên: Pao", "Cách xưng hô: Pao - B-boys". Su có `memory_commitment` và `relationship: xưng hô: Chào Su` (rác).
5. **Pao lặp "B-boys."** ở cuối 9/13 câu trả lời — bản méo của "B-baka" trong few-shot (`personas.py`). Câu đầu còn tự
   xưng "Mình". (Chêm "Baka/Urusai" và đổi sang "tớ" khi thật lòng là đúng hồ sơ nhân vật.)
6. **Prompt ảnh sau khi dịch:** ảnh Pao có tag `1pao` (lẽ ra `1girl`) và bối cảnh "Chinese ink art studio" (Pao học ở
   Tokyo). Ảnh Pao cầm cọ đưa lên miệng chứ không đang vẽ; ảnh Su không thấy đang viết. Ảnh gửi vào hội thoại không kèm
   câu nào của nhân vật — chú thích là chính câu xin của người dùng.

**Kết luận / cần đổi gì trong code** (đã sửa trong code cùng ngày — DECISIONS D25, D26, D27 và `/app`; chưa chạy lại kịch bản này)
- Ưu tiên cao nhất là lỗi 4: hỏng đúng trụ 2 "trò chuyện có trí nhớ". Prompt trích fact phải chỉ lấy điều **về người dùng**,
  giới hạn bộ khoá, và không lưu suy diễn kiểu `boundary`. Fact sai đang được đưa ngược vào prompt nên có thể làm nhân vật
  "quên" con chó.
- Lỗi 1 và 2 làm hỏng trải nghiệm ngay lần đầu dùng; lỗi 3 làm người dùng tưởng tính năng trí nhớ không chạy.
- Tốc độ chat chấp nhận được (~4 s/câu). Vẽ ảnh 40–99 s ở mức "Tốt nhất"; chưa thử các mức thấp hơn.
- `reloads` 5/20 lượt, đọc từng dòng `data/perf.jsonl`: 2 lần khởi động lạnh (26/10 và 27/10, 22–30 s); 1 lần ngay sau
  ảnh đầu (13:30, chờ chữ đầu 9 s — D16 nhả VRAM); và **2 lần giữa lúc chat thuần** (13:33 chờ 16 s, 13:34 chờ 32 s).
  Khả năng cao do **tóm tắt hội thoại**: `summarize_conversation()` gọi `get_translate_model()` → `qwen2.5:3b`, nên Ollama đổi
  model qua lại trên GPU 4 GB — đúng hệ quả D24 dặn theo dõi. Khớp với số liệu: hội thoại Pao (29 tin) có `memory_summary`
  và 2 lần nạp lại; hội thoại Su (15 tin) không có tóm tắt và không lần nào. (Trích fact dùng chính `OLLAMA_MODEL` cùng
  `num_ctx` nên không gây nạp lại.) Chưa kiểm bằng log Ollama. Một lượt chờ 32 s là dễ thấy; đáng thử
  `YUMEAI_TRANSLATE_MODEL=qwen3:4b-instruct` sau khi đo chất lượng dịch prompt ảnh của qwen3.

---

## 2026-10-26 — NVIDIA GeForce RTX 3050 Laptop GPU 4096 MiB, RAM 31.7 GiB — so sánh model chat (bench_chat)

**Phần cứng**
- GPU: NVIDIA GeForce RTX 3050 Laptop GPU
- RAM: 31.7 GiB
- HĐH: Windows-10-10.0.26200-SP0; Python 3.11.16

**Cấu hình chạy**
- Model: qwen2.5:3b, qwen3:4b-instruct
- num_ctx 4096, num_predict 220, temperature 0.85, num_gpu Ollama tự quyết, few-shot bật, cửa sổ 12 tin
- Bộ thoại cố định: Pao, Su — 12 câu × 2 vòng, seed 20261022+vòng; trí nhớ giả 3 fact; nhả model trước/sau: có
- ComfyUI: tắt

**Kết quả**
| Model | Nạp cold | Bộ nhớ | Token/giây | Chờ chữ đầu | Cả câu | Giữ vai | Ghi chú |
|---|---|---|---|---|---|---|---|
| qwen2.5:3b | 3.7s | 2.0 GiB, 100% GPU | 64.4 | 0.1s | 0.8s | 17/24 | trượt: Không chữ Hán, Không liệt kê, Xưng hô, Độ dài, Nhớ đúng |
| qwen3:4b-instruct | 6.3s | 3.3 GiB, 67% GPU | 14.9 | 0.5s | 4.2s | 23/24 | trượt: Nhớ đúng |

Trung vị trên mọi câu; "Giữ vai" = số câu đạt mọi luật / số câu đã trả lời.

**Giữ vai theo từng luật** (đạt/áp dụng)
| Model | Tiếng Việt | Không chữ Hán | Không emoji | Không liệt kê | Không nhận AI | Xưng hô | Độ dài | Nhớ đúng |
|---|---|---|---|---|---|---|---|---|
| qwen2.5:3b | 24/24 | 23/24 | 24/24 | 23/24 | 24/24 | 23/24 | 23/24 | 0/4 |
| qwen3:4b-instruct | 24/24 | 24/24 | 24/24 | 24/24 | 24/24 | 24/24 | 24/24 | 3/4 |

**Ghi chú**
- Số đo tự động: `python bench_chat.py --models qwen2.5:3b qwen3:4b-instruct`; toàn bộ câu trả lời: `data/bench/chat_20261026-163537.json`.
- Luật chỉ bắt lỗi thô; đọc câu trả lời trong file JSON rồi mới kết luận.
- Đã sửa tay hai số của qwen3:4b-instruct (22/24 → 23/24, "Không nhận AI" 23/24 → 24/24): luật bắt nhầm "…không hỏi tên tao là ai" — "ai" ở đây là đại từ, không phải "AI". Luật đã sửa (chỉ khớp chữ HOA "AI"), chấm lại file JSON bằng luật mới: các số khác không đổi.
- Ollama chạy từ ổ rời (`E:\Ollama`, cổng 11435, bản 0.35.0) qua `E:\duan\_vscode\SO-SANH-MODEL-CHAT.cmd`.

**Kết luận**
- **qwen3:4b-instruct giữ vai tốt hơn rõ:** 23/24 so với 17/24, nhớ đúng 3/4 so với 0/4. Đọc câu trả lời: qwen2.5:3b lẫn cả câu tiếng Trung khi bị hỏi "em là AI", bịa đồ uống ("trà hoa cúc" thay vì trà sữa ít đường), gạch đầu dòng khi bị dụ liệt kê, có câu thô tục; qwen3 trả lời liền mạch, đúng giọng, nhớ tên và đồ uống.
- **Đổi lại chậm ~4 lần trên GPU 4 GB:** 14.9 so với 64 token/giây, cả câu ~4 giây so với ~1 giây, vì 3.3 GiB không vừa hết VRAM (67% trên GPU, phần còn lại chạy CPU), và chiếm VRAM nhiều hơn khi phải nhường cho ComfyUI. Dự kiến trên GPU từ 6 GB trở lên thì nằm trọn GPU (chưa đo).
- Chủ dự án đồng ý: `OLLAMA_MODEL` mặc định đổi sang `qwen3:4b-instruct` cùng ngày (DECISIONS D24). Cần nhanh thì `MO-PAOCAKE.cmd --model qwen2.5:3b`.

---

## 2026-09-11 — Chuẩn bị và kiểm tra file Wan 2.2 5B

**Phần cứng**
- GPU: NVIDIA GeForce RTX 3050 Laptop GPU, 4GB VRAM.
- RAM: khoảng 32GB (31,7 GiB hệ thống báo).
- HĐH: Windows x64; Python 3.12.14 trên ổ rời E:.

**Cấu hình kiểm tra**
- Bộ Comfy-Org: wan2.2_ti2v_5B_fp16, wan2.2_vae, umt5_xxl_fp8_e4m3fn_scaled.
- Không khởi động Ollama, server ComfyUI hoặc chạy sinh video; không thay biến cấu hình ứng dụng YumeAI.

**Kết quả**
| Hạng mục | Kết quả | Thời gian | Ghi chú |
|---|---|---|---|
| SHA-256 và kích thước 3 model | Đạt | Không đo benchmark | Khớp metadata từ kho chính thức |
| Cấu trúc safetensors | Đạt | — | Đọc header và danh sách tensor, không nạp toàn bộ weights lên GPU |
| ComfyUI model discovery | Đạt | — | Tìm được đúng 3 tên file trong các nhóm model |
| Node của workflow Wan 2.2 5B | Đạt | — | Core + nodes_wan, nodes_video, nodes_model_advanced |
| Sinh video và hiệu năng | Chưa thử | — | Chưa có kết luận khả năng chạy trên 4GB |

**Ghi chú**
- PyTorch hiện có cảnh báo backend CUDA tối ưu cần cu130 trở lên; kiểm tra đăng ký node vẫn đạt. Không thay bản PyTorch trong đợt tải model.
- Workflow chính thức và bản thử nhỏ nằm trong ComfyUI/user/default/workflows. Chi tiết nguồn, SHA-256 và revision ở E:/Python/manifests/wan22-5b.json.
- Người dùng muốn phát triển cấu hình xử lý cho nhiều loại máy ở bước sau; chưa triển khai hệ thống tự chọn cấu hình trong đợt này.

---

## Mẫu — copy khối này rồi điền

```markdown
## YYYY-MM-DD — <tên/mô tả máy>

**Phần cứng**
- GPU:            (vd RTX 3060 12GB rời / RTX 3050 Laptop 4GB / Radeon 680M iGPU — máy cũ)
- VRAM:
- RAM:
- HĐH:

**Cấu hình chạy**
- OLLAMA_MODEL:
- Model ComfyUI (checkpoint / Kontext quant):
- Biến môi trường đã đổi:      (vd YUMEAI_LOW_VRAM_MODE=0)

**Kết quả**
| Hạng mục | Kết quả | Thời gian | Ghi chú |
|---|---|---|---|
| `python test_router.py` | pass / fail | | |
| Chat (chất lượng giữ vai) | | | |
| text2img SD1.5 | | ~?s | |
| Flux Kontext đổi đồ | | ~?s | |
| mask+inpaint (SAM) | | ~?s | |
| Đổi đồ 2 nhân vật (2-pass) | | ~?s | |
| SDXL (nếu có thử) | | | |

**Lỗi gặp phải**
- (dán nguyên văn thông báo lỗi, càng đầy đủ càng tốt)

**Kết luận / cần đổi gì trong code**
-
```

---

## Tham chiếu máy phát triển trước đây

Máy phát triển **trước đây** (trước 11/9/2026): AMD Radeon 680M (iGPU), 4GB VRAM chia từ
16GB RAM, hệ điều hành thấy ~12.4GB. Máy đang dùng: [`MAY.md`](MAY.md). Số liệu tham chiếu
từ máy Radeon cũ:

| Hạng mục | Thời gian |
|---|---|
| Flux Kontext một ảnh | ~400 giây |
| Vision (qwen2.5vl) một lần mô tả ảnh | hàng chục giây |
| Đổi đồ 2 nhân vật (trước khi cache vision) | ~8 lần gọi vision |
| Đổi đồ 2 nhân vật (sau khi cache) | ~4 lần gọi vision |

**Những thứ chưa bao giờ chạy được trên máy Radeon cũ (và chưa thử trên RTX 3050 4 GB) — cần test khi có máy mạnh:**

- [ ] SDXL checkpoint (hiện `get_checkpoint_model()` lọc bỏ có chủ đích)
- [ ] Flux Kontext quant Q5/Q6 (chỉ cần bỏ file vào `ComfyUI/models/unet`, code tự chọn)
- [ ] `YUMEAI_LOW_VRAM_MODE=0` — bỏ cap 24 steps
- [ ] ComfyUI **không** dùng `--lowvram`
- [ ] Train LoRA nhân vật (chìa khoá cho character consistency — xem `STATE.md` mục 3)
- [ ] Ollama model 7B trở lên (`qwen2.5:7b`)
- [ ] Chạy 2 job ComfyUI song song (nới Resource Guard)
