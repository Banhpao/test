"""
Test nhanh engine ĐỔI ĐỒ TỰ ĐỘNG (auto_outfit_edit_fn) — không cần mở UI.

Cách dùng (PowerShell, trong thư mục project, ComfyUI + Ollama đang chạy):
    python test_auto_outfit.py "duong_dan_anh_goc.png" "đổi thành đồ thể thao" "ket_qua.png"

- Tham số 1: ảnh nhân vật gốc (png/jpg)
- Tham số 2: prompt đổi đồ (tiếng Việt)  — mặc định "đổi thành đồ thể thao"
- Tham số 3: nơi lưu ảnh kết quả          — mặc định "auto_outfit_result.png"

Xong thì MỞ ảnh kết quả để tự đánh giá: có giữ mặt/tóc/tỉ lệ/tư thế/nền và CHỈ đổi
quần áo không? Có bị chibi không? (Về lý thuyết mask khoá vùng nên KHÔNG thể chibi.)
"""
import sys
from PIL import Image
import app_gradio as A

img_path = sys.argv[1] if len(sys.argv) > 1 else "input.png"
request  = sys.argv[2] if len(sys.argv) > 2 else "đổi thành đồ thể thao"
out_path = sys.argv[3] if len(sys.argv) > 3 else "auto_outfit_result.png"

print(f"📥 Ảnh gốc : {img_path}")
print(f"📝 Yêu cầu : {request}")
print("⏳ Đang chạy: vision dò quần áo → SAM mask → inpaint (SD ~30-90s)...\n")

img = Image.open(img_path)
result, info = A.auto_outfit_edit_fn(img, request, style="Anime", resolution=768)
result.save(out_path)

print("\n" + "=" * 55)
print(info)
print("=" * 55)
print(f"✅ ĐÃ LƯU: {out_path}  ({result.size[0]}x{result.size[1]})")
print("👉 Mở ảnh này lên xem: có giữ form + chỉ đổi quần áo + KHÔNG chibi không?")
