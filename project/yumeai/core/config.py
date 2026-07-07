"""
YumeAI Core Config
Cấu hình chung cho Ollama, ComfyUI và đường dẫn model.
Có thể override bằng biến môi trường.
"""
import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")

COMFY_MODELS_DIR = os.getenv(
    "COMFY_MODELS_DIR",
    r"D:\baitap\code py\formyhorny\ComfyUI\models"
)
