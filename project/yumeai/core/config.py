"""
YumeAI Core Config
Cấu hình chung cho Ollama, ComfyUI và đường dẫn model.
Có thể override bằng biến môi trường.
"""
import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")

# Timeout (giây) chờ ComfyUI vẽ xong 1 job — nguồn DUY NHẤT cho app_gradio.py + main.py
# (máy 4GB VRAM vẽ ControlNet/Flux/upscale có thể >5 phút). Chỉnh qua biến môi trường:
#   $env:YUMEAI_COMFY_TIMEOUT_SEC="1800"
COMFY_TIMEOUT_SEC = int(os.getenv("YUMEAI_COMFY_TIMEOUT_SEC", "1800"))

# Timeout (giây) cho bước VERIFY ảnh bằng vision model (qwen2.5vl) sau khi Flux vẽ xong.
# Máy 4GB VRAM vừa chạy Flux xong thì vision chạy chậm → 150s cũ dễ timeout, oan ảnh tốt.
#   $env:YUMEAI_VERIFY_TIMEOUT_SEC="300"
VERIFY_TIMEOUT_SEC = int(os.getenv("YUMEAI_VERIFY_TIMEOUT_SEC", "300"))

COMFY_MODELS_DIR = os.getenv(
    "COMFY_MODELS_DIR",
    r"D:\baitap\code py\formyhorny\ComfyUI\models"
)
