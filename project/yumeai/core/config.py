"""
YumeAI Core Config
Cấu hình chung cho Ollama, ComfyUI và đường dẫn model.
Có thể override bằng biến môi trường.
"""
import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")

# Dữ liệu sản phẩm (hội thoại, trí nhớ, gallery) nằm ngoài source code để có thể
# backup/copy độc lập. SQLite là chủ đích: dự án local một người dùng không cần
# thêm một tiến trình Postgres chỉ để lưu vài nghìn tin nhắn.
DATA_DIR = os.path.abspath(os.getenv(
    "YUMEAI_DATA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"),
))
DATABASE_PATH = os.path.abspath(os.getenv(
    "YUMEAI_DATABASE_PATH",
    os.path.join(DATA_DIR, "yumeai.db"),
))
GENERATED_IMAGES_DIR = os.path.abspath(os.getenv(
    "YUMEAI_GENERATED_IMAGES_DIR",
    os.path.join(DATA_DIR, "images"),
))

# Timeout (giây) chờ ComfyUI vẽ xong 1 job — nguồn DUY NHẤT cho app_gradio.py + main.py
# (máy 4GB VRAM vẽ ControlNet/Flux/upscale có thể >5 phút). Chỉnh qua biến môi trường:
#   $env:YUMEAI_COMFY_TIMEOUT_SEC="1800"
COMFY_TIMEOUT_SEC = int(os.getenv("YUMEAI_COMFY_TIMEOUT_SEC", "1800"))

# Timeout (giây) cho bước VERIFY ảnh bằng vision model (qwen2.5vl) sau khi Flux vẽ xong.
# Máy 4GB VRAM vừa chạy Flux xong thì vision chạy chậm → 150s cũ dễ timeout, oan ảnh tốt.
#   $env:YUMEAI_VERIFY_TIMEOUT_SEC="300"
VERIFY_TIMEOUT_SEC = int(os.getenv("YUMEAI_VERIFY_TIMEOUT_SEC", "300"))

# ✨ Đổi đồ trong CHAT: ưu tiên mask+inpaint (giữ form, chống chibi, nhanh) thay vì Flux Kontext
# khi máy có node segment-anything. Đặt "0" để quay lại Flux Kontext như cũ.
#   $env:YUMEAI_AUTO_MASK_EDIT="0"
AUTO_MASK_EDIT = os.getenv("YUMEAI_AUTO_MASK_EDIT", "1").strip().lower() not in {"0", "false", "off", "no"}

# ✨ Nâng chất lượng auto mask-inpaint (đều thiếu node thì tự bỏ qua, không lỗi).
#   HIRES FIX : vẽ lại vùng đồ ở 1.5x (chi tiết THẬT: nếp vải, đường may). MẶC ĐỊNH BẬT —
#               đã sửa bug tô xám (chạy 1.5x an toàn 4GB, dùng conditioning gốc). Tắt: "0".
#   UPSCALE   : ESRGAN x4 → thu về 2x cho nét cuối (an toàn, MẶC ĐỊNH BẬT).
#   CONTROLNET: canny khóa nét. MẶC ĐỊNH TẮT — trên SD1.5 + ảnh nhỏ dễ washout màu.
#               Bật để thử:  $env:YUMEAI_AUTO_MASK_CONTROLNET="1"
AUTO_MASK_UPSCALE = os.getenv("YUMEAI_AUTO_MASK_UPSCALE", "1").strip().lower() not in {"0", "false", "off", "no"}
AUTO_MASK_CONTROLNET = os.getenv("YUMEAI_AUTO_MASK_CONTROLNET", "0").strip().lower() not in {"0", "false", "off", "no"}
AUTO_MASK_HIRES = os.getenv("YUMEAI_AUTO_MASK_HIRES", "1").strip().lower() not in {"0", "false", "off", "no"}

# ══════════════════════════════════════════════════════════════════════════
# CHAT — cấu hình dùng CHUNG cho app_gradio.py và main.py (xem chat_context.py)
# ══════════════════════════════════════════════════════════════════════════
# Số message lịch sử tối đa đưa vào ngữ cảnh mỗi lượt chat. Quá nhiều thì tràn
# context window và model bắt đầu trả lời lạc đề mà KHÔNG báo lỗi gì.
#   $env:YUMEAI_CHAT_HISTORY_MAX="12"
# Timeout (giây) cho một lượt chat streaming. Trước đây app_gradio.py dùng 30s còn
# main.py dùng 120s — cùng một model mà hai cửa vào bỏ cuộc ở hai mốc khác nhau.
#   $env:YUMEAI_CHAT_TIMEOUT_SEC="120"
# ── Tham số sinh câu trả lời. Model nhỏ rất dễ lặp từ/lặp câu nên cần
# repeat_penalty; top_p giữ câu tự nhiên mà không lan man.
CHAT_TEMPERATURE = float(os.getenv("YUMEAI_CHAT_TEMPERATURE", "0.85"))
CHAT_TOP_P = float(os.getenv("YUMEAI_CHAT_TOP_P", "0.92"))
CHAT_REPEAT_PENALTY = float(os.getenv("YUMEAI_CHAT_REPEAT_PENALTY", "1.12"))
CHAT_NUM_PREDICT = int(os.getenv("YUMEAI_CHAT_NUM_PREDICT", "220"))

# Few-shot: chèn vài lượt thoại mẫu trước lịch sử để model nhỏ khỏi tuột vai.
# Tắt nếu muốn tiết kiệm context: $env:YUMEAI_CHAT_FEWSHOT="0"
CHAT_FEWSHOT = os.getenv("YUMEAI_CHAT_FEWSHOT", "1").strip().lower() not in {"0", "false", "off", "no"}

# Trí nhớ dài: nén các lượt đã rơi ra khỏi cửa sổ lịch sử thành vài gạch đầu dòng
# rồi nhét vào system prompt. Đây là thứ tạo cảm giác "nhân vật nhớ mình".
CHAT_SUMMARY = os.getenv("YUMEAI_CHAT_SUMMARY", "1").strip().lower() not in {"0", "false", "off", "no"}
# Cứ thêm bấy nhiêu message bị đẩy ra ngoài cửa sổ thì mới tóm tắt lại một lần
# (tóm tắt mỗi lượt sẽ tốn thêm một lời gọi LLM cho mỗi tin nhắn).
CHAT_SUMMARY_EVERY = int(os.getenv("YUMEAI_CHAT_SUMMARY_EVERY", "6"))
# Số message tối đa đưa vào MỘT lần tóm tắt. Không có cận này thì overflow lớn dần
# vô hạn: chat 200 lượt là mỗi lần tóm tắt phải nuốt gần 200 message, càng lúc càng
# chậm, và summarize_conversation() cắt bớt ở phía sau nên phần MỚI NHẤT bị vứt đi
# âm thầm — trí nhớ dài đóng băng ở đoạn đầu cuộc trò chuyện.
# Các fact bền vững nằm ở bảng memory_facts (yumeai/services/memory_service.py),
# nên hàm này chỉ cần lo phần hội thoại vừa rơi khỏi cửa sổ.
CHAT_SUMMARY_MAX_MESSAGES = max(
    CHAT_SUMMARY_EVERY,
    int(os.getenv("YUMEAI_CHAT_SUMMARY_MAX_MESSAGES", "40")),
)

CHAT_TIMEOUT_SEC = int(os.getenv("YUMEAI_CHAT_TIMEOUT_SEC", "120"))

CHAT_HISTORY_MAX_MESSAGES = int(os.getenv("YUMEAI_CHAT_HISTORY_MAX", "12"))

# Context window cho chat. Máy 4GB VRAM: 4096 là an toàn; hạ xuống 2048 nếu thấy
# Ollama đẩy model ra RAM (chạy chậm hẳn).
#   $env:YUMEAI_CHAT_NUM_CTX="4096"
CHAT_NUM_CTX = int(os.getenv("YUMEAI_CHAT_NUM_CTX", "4096"))

# Trích memory fact theo LÔ sau khi đã trả lời xong, không gọi thêm LLM cho mỗi
# message. Mặc định 12 text message ≈ 6 lượt chat, phù hợp máy chỉ có ~12GB RAM.
MEMORY_FACTS_ENABLED = os.getenv(
    "YUMEAI_MEMORY_FACTS", "1"
).strip().lower() not in {"0", "false", "off", "no"}
MEMORY_EXTRACTION_EVERY_MESSAGES = max(
    4, int(os.getenv("YUMEAI_MEMORY_EVERY", "12"))
)
MEMORY_EXTRACTION_BATCH_SIZE = max(
    MEMORY_EXTRACTION_EVERY_MESSAGES,
    int(os.getenv("YUMEAI_MEMORY_BATCH_SIZE", "24")),
)
MEMORY_EXTRACTION_TIMEOUT_SEC = max(
    20, int(os.getenv("YUMEAI_MEMORY_TIMEOUT_SEC", "90"))
)

# Trước đây hardcode r"D:\baitap\code py\formyhorny\ComfyUI\models" nên chỉ đúng
# trên đúng một máy Windows. Nay tự dò theo vị trí package, vẫn override được bằng env.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMFY_MODELS_DIR = os.getenv(
    "COMFY_MODELS_DIR",
    os.path.join(_PROJECT_ROOT, "ComfyUI", "models"),
)
