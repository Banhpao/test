"""
Vá lỗi GroundingDINO không tương thích transformers 5.x
Lỗi: 'BertModel' object has no attribute 'get_head_mask'

Chạy: python fix_dino.py
"""
import os
import re

# Đường dẫn file bertwarper.py
BASE = r"D:\baitap\code py\formyhorny\ComfyUI\custom_nodes\comfyui_segment_anything\local_groundingdino\models\GroundingDINO"
TARGET = os.path.join(BASE, "bertwarper.py")


def patch():
    if not os.path.exists(TARGET):
        print(f"❌ Không tìm thấy file: {TARGET}")
        print("   Kiểm tra lại đường dẫn cài comfyui_segment_anything.")
        return

    with open(TARGET, "r", encoding="utf-8") as f:
        src = f.read()

    original = src
    changes = []

    # ── Fix 1: get_head_mask không còn tồn tại trong transformers 5.x ──
    # self.get_head_mask = bert_model.get_head_mask
    if "self.get_head_mask = bert_model.get_head_mask" in src:
        src = src.replace(
            "self.get_head_mask = bert_model.get_head_mask",
            "self.get_head_mask = getattr(bert_model, 'get_head_mask', "
            "lambda head_mask, num_layers, is_attention_chunked=False: [None] * num_layers)"
        )
        changes.append("Fix 1: get_head_mask → getattr an toàn")

    # ── Fix 2: get_extended_attention_mask bỏ tham số device ──
    # Tìm các pattern gọi có device
    patterns = [
        (r"self\.get_extended_attention_mask\(\s*attention_mask,\s*input_shape,\s*device\s*\)",
         "self.get_extended_attention_mask(attention_mask, input_shape)"),
        (r"self\.get_extended_attention_mask\(\s*attention_mask,\s*input_shape,\s*device=device\s*\)",
         "self.get_extended_attention_mask(attention_mask, input_shape)"),
        (r"get_extended_attention_mask\(\s*attention_mask,\s*input_shape,\s*device\s*\)",
         "get_extended_attention_mask(attention_mask, input_shape)"),
    ]
    for pat, repl in patterns:
        if re.search(pat, src):
            src = re.sub(pat, repl, src)
            changes.append("Fix 2: get_extended_attention_mask bỏ device")
            break

    if src == original:
        print("ℹ️  File đã được vá trước đó hoặc không khớp pattern.")
        print("   Nếu vẫn lỗi, dùng cách 2: hạ cấp transformers.")
        return

    # Backup file gốc
    backup = TARGET + ".backup"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"💾 Đã backup: {backup}")

    # Ghi file đã vá
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(src)

    print("✅ Vá thành công!")
    for c in changes:
        print(f"   • {c}")
    print("\n→ Restart ComfyUI rồi thử lại.")


if __name__ == "__main__":
    patch()