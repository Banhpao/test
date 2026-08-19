# Nhật ký test — YumeAI

Nơi ghi kết quả chạy thử **trên máy khác** (máy mạnh hơn, GPU rời, cấu hình khác).
Người/AI đọc file này để biết cái gì thực sự chạy được ở đâu, không phải đoán.

Mục mới nhất ở **trên cùng**.

> **Vì sao cần file này:** máy phát triển chính dùng iGPU AMD Radeon 680M với 4GB
> "VRAM" cắt từ RAM (xem `DECISIONS.md` D3). Rất nhiều giới hạn trong code là do
> phần cứng đó. Khi test trên máy khác, ghi lại đây để biết giới hạn nào là **thật**
> và giới hạn nào chỉ là **do máy yếu**.

---

## Mẫu — copy khối này rồi điền

```markdown
## YYYY-MM-DD — <tên/mô tả máy>

**Phần cứng**
- GPU:            (vd RTX 3060 12GB rời / Radeon 680M iGPU)
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

## Chưa có mục nào

Máy phát triển hiện tại: AMD Radeon 680M (iGPU), 4GB VRAM chia từ 16GB RAM,
hệ điều hành thấy ~12.4GB. Số liệu tham chiếu từ máy này:

| Hạng mục | Thời gian |
|---|---|
| Flux Kontext một ảnh | ~400 giây |
| Vision (qwen2.5vl) một lần mô tả ảnh | hàng chục giây |
| Đổi đồ 2 nhân vật (trước khi cache vision) | ~8 lần gọi vision |
| Đổi đồ 2 nhân vật (sau khi cache) | ~4 lần gọi vision |

**Những thứ chưa bao giờ chạy được trên máy này — cần test khi có máy mạnh:**

- [ ] SDXL checkpoint (hiện `get_checkpoint_model()` lọc bỏ có chủ đích)
- [ ] Flux Kontext quant Q5/Q6 (chỉ cần bỏ file vào `ComfyUI/models/unet`, code tự chọn)
- [ ] `YUMEAI_LOW_VRAM_MODE=0` — bỏ cap 24 steps
- [ ] ComfyUI **không** dùng `--lowvram`
- [ ] Train LoRA nhân vật (chìa khoá cho character consistency — xem `STATE.md` mục 3)
- [ ] Ollama model 7B trở lên (`qwen2.5:7b`)
- [ ] Chạy 2 job ComfyUI song song (nới Resource Guard)
