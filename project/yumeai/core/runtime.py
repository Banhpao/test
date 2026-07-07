"""
YumeAI Runtime Config + Resource Guard
Dùng chung cho MỌI entrypoint (app_gradio.py, main.py, ...) — trước đây mỗi file
tự định nghĩa lại cấu hình low-VRAM và cái khóa chống chạy chồng job, dễ bị lệch
pha khi sửa một chỗ mà quên chỗ kia.

Cấu hình (đọc từ biến môi trường, ví dụ trong PowerShell trước khi chạy app):
  $env:YUMEAI_LOW_VRAM_MODE="1"               # 1=giảm tải, 0=giữ như cũ
  $env:YUMEAI_FLUX_SAFE_STEPS="24"            # cap steps cho Flux thường
  $env:YUMEAI_GARMENT_SAFE_STEPS="24"         # cap steps cho teacher/garment pipeline
  $env:YUMEAI_TEACHER_RETRY_MAX="0"           # 0=không retry tự động để tránh chạy chồng lâu
  $env:YUMEAI_RESOURCE_GUARD="1"              # 1=chỉ cho 1 job ComfyUI chạy cùng lúc
  $env:YUMEAI_STYLE_INTENT_GUARD="1"          # 1=tự chặn chibi ngoài ý muốn
  $env:YUMEAI_GARMENT_SOFT_REFERENCE="1"      # 1=ảnh outfit chỉ tham chiếu quần áo
  $env:YUMEAI_GARMENT_PRESERVE_GUIDANCE="4.2"
  $env:YUMEAI_GARMENT_PRESERVE_DENOISE="0.78"

QUAN TRỌNG — Resource Guard 2 lớp:
1) Lớp trong tiến trình (threading.RLock): chặn nhiều luồng/request trong CÙNG
   một tiến trình Python (ví dụ nhiều tab trình duyệt gọi cùng 1 app_gradio.py).
2) Lớp liên-tiến-trình (file lock qua thư viện `filelock`): chặn cả trường hợp
   bạn chạy `python app_gradio.py` VÀ `python main.py` là HAI tiến trình riêng
   biệt cùng lúc — quan trọng với máy 4GB VRAM vì ComfyUI dùng chung 1 GPU cho
   cả hai. Cần cài: pip install filelock
   Nếu CHƯA cài filelock, app vẫn chạy bình thường nhưng chỉ có lớp (1) —
   sẽ có cảnh báo in ra khi khởi động.
"""
import os
import tempfile
import threading
import time

from yumeai.services.comfy_service import submit_workflow as _raw_submit_workflow
from yumeai.services.comfy_service import poll_and_download as _raw_poll_and_download

try:
    from filelock import FileLock, Timeout as _FileLockTimeout
    _HAS_FILELOCK = True
except ImportError:
    _HAS_FILELOCK = False
    print(
        "⚠️ Chưa cài 'filelock' (pip install filelock) — Resource Guard chỉ chặn được "
        "trong CÙNG một tiến trình. Nếu chạy app_gradio.py và main.py SONG SONG (2 lệnh "
        "python khác nhau), guard sẽ KHÔNG thấy nhau và có thể tràn 4GB VRAM. "
        "Cài filelock để bật khóa liên-tiến-trình."
    )

# File khóa dùng chung toàn hệ thống — mọi tiến trình YumeAI (app_gradio.py, main.py,
# hoặc bất kỳ script nào import yumeai.core.runtime) đều tranh nhau CÙNG 1 file này.
_CROSS_PROCESS_LOCK_PATH = os.path.join(tempfile.gettempdir(), "yumeai_comfyui_job.lock")
_cross_process_lock = FileLock(_CROSS_PROCESS_LOCK_PATH, timeout=0) if _HAS_FILELOCK else None



# ══════════════════════════════════
# ENV HELPERS
# ══════════════════════════════════
def _env_bool(name: str, default: bool = True) -> bool:
    v = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return v not in {"0", "false", "off", "no", "n"}


def _env_int(name: str, default: int, *, lo: int = None, hi: int = None) -> int:
    try:
        v = int(os.getenv(name, str(default)))
    except Exception:
        v = int(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _env_float(name: str, default: float, *, lo: float = None, hi: float = None) -> float:
    try:
        v = float(os.getenv(name, str(default)))
    except Exception:
        v = float(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


YUMEAI_LOW_VRAM_MODE = _env_bool("YUMEAI_LOW_VRAM_MODE", True)
YUMEAI_RESOURCE_GUARD = _env_bool("YUMEAI_RESOURCE_GUARD", True)
YUMEAI_FLUX_SAFE_STEPS = _env_int("YUMEAI_FLUX_SAFE_STEPS", 24, lo=8, hi=40)
YUMEAI_GARMENT_SAFE_STEPS = _env_int("YUMEAI_GARMENT_SAFE_STEPS", 24, lo=8, hi=40)
YUMEAI_TEACHER_RETRY_MAX = _env_int("YUMEAI_TEACHER_RETRY_MAX", 0 if YUMEAI_LOW_VRAM_MODE else 1, lo=0, hi=1)
YUMEAI_STYLE_INTENT_GUARD = _env_bool("YUMEAI_STYLE_INTENT_GUARD", True)
YUMEAI_GARMENT_SOFT_REFERENCE = _env_bool("YUMEAI_GARMENT_SOFT_REFERENCE", True)
YUMEAI_GARMENT_PRESERVE_GUIDANCE = _env_float("YUMEAI_GARMENT_PRESERVE_GUIDANCE", 4.2, lo=1.0, hi=5.8)
YUMEAI_GARMENT_PRESERVE_DENOISE = _env_float("YUMEAI_GARMENT_PRESERVE_DENOISE", 0.78, lo=0.45, hi=1.0)


def _resource_safe_steps(requested_steps, *, kind: str = "flux") -> int:
    """Cap steps để Flux/Kontext không ép laptop 4GB VRAM chạy quá lâu."""
    steps = max(8, min(40, int(requested_steps)))
    if not YUMEAI_LOW_VRAM_MODE:
        return steps
    cap = YUMEAI_GARMENT_SAFE_STEPS if kind in {"garment", "teacher"} else YUMEAI_FLUX_SAFE_STEPS
    capped = max(8, min(steps, int(cap)))
    if capped < steps:
        print(f"🛡️ low-VRAM: cap {kind} steps {steps} → {capped}")
    return capped


# ══════════════════════════════════
# RESOURCE GUARD — chặn nhiều job ComfyUI chạy chồng trong CÙNG một tiến trình
# ══════════════════════════════════
class ResourceBusyError(Exception):
    """Raised khi đang có job ComfyUI khác chạy và Resource Guard đang bật.
    Mỗi entrypoint tự bắt exception này và đổi sang kiểu lỗi phù hợp với mình
    (gr.Error cho Gradio, HTTPException cho FastAPI...) — runtime.py không phụ
    thuộc vào framework nào để main.py không cần cài gradio."""
    pass


_YUMEAI_JOB_LOCK = threading.RLock()
_YUMEAI_JOB_STATE = {"owner": None, "label": "", "start": 0.0, "depth": 0}


def acquire_generation_slot(label: str = "ComfyUI workflow") -> bool:
    """Non-blocking, 2 lớp:
    1) In-process: đang có job thì báo bận, không gửi thêm workflow mới.
    2) Cross-process (nếu có filelock): chặn cả tiến trình khác (vd main.py
       đang chạy song song với app_gradio.py) cùng gửi job lên ComfyUI.
    """
    if not YUMEAI_RESOURCE_GUARD:
        return False
    tid = threading.get_ident()
    if _YUMEAI_JOB_STATE.get("owner") == tid:
        _YUMEAI_JOB_STATE["depth"] += 1
        return True

    if not _YUMEAI_JOB_LOCK.acquire(blocking=False):
        active = _YUMEAI_JOB_STATE.get("label") or "ComfyUI workflow"
        elapsed = int(max(0, time.time() - float(_YUMEAI_JOB_STATE.get("start") or time.time())))
        raise ResourceBusyError(
            f"⚠️ YumeAI đang xử lý job khác (cùng tiến trình): {active} ({elapsed}s). "
            "Để tránh tràn VRAM/RAM hoặc reset máy, đã chặn gửi thêm job ComfyUI. "
            "Đợi job hiện tại xong rồi thử lại."
        )

    if _HAS_FILELOCK:
        try:
            _cross_process_lock.acquire(timeout=0)
        except _FileLockTimeout:
            _YUMEAI_JOB_LOCK.release()
            raise ResourceBusyError(
                "⚠️ Một tiến trình YumeAI KHÁC (ví dụ app_gradio.py hoặc main.py đang chạy "
                "song song trong tiến trình khác) đang xử lý job ComfyUI. Để tránh tràn 4GB "
                "VRAM, đã chặn gửi thêm job. Đợi tiến trình kia xong rồi thử lại."
            )

    _YUMEAI_JOB_STATE.update({"owner": tid, "label": label, "start": time.time(), "depth": 1})
    print(f"🛡️ Resource Guard: START {label}")
    return True


def release_generation_slot() -> None:
    if not YUMEAI_RESOURCE_GUARD:
        return
    tid = threading.get_ident()
    if _YUMEAI_JOB_STATE.get("owner") != tid:
        return
    _YUMEAI_JOB_STATE["depth"] = max(0, int(_YUMEAI_JOB_STATE.get("depth") or 1) - 1)
    if _YUMEAI_JOB_STATE["depth"] > 0:
        return
    label = _YUMEAI_JOB_STATE.get("label") or "ComfyUI workflow"
    elapsed = int(max(0, time.time() - float(_YUMEAI_JOB_STATE.get("start") or time.time())))
    _YUMEAI_JOB_STATE.update({"owner": None, "label": "", "start": 0.0, "depth": 0})
    if _HAS_FILELOCK:
        try:
            _cross_process_lock.release()
        except RuntimeError:
            pass
    try:
        _YUMEAI_JOB_LOCK.release()
    except RuntimeError:
        pass
    print(f"🛡️ Resource Guard: END {label} ({elapsed}s)")


def guarded_submit_workflow(workflow, *args, **kwargs):
    """Wrapper submit_workflow có Resource Guard — dùng chung cho mọi entrypoint."""
    acquire_generation_slot("ComfyUI workflow")
    try:
        return _raw_submit_workflow(workflow, *args, **kwargs)
    except Exception:
        release_generation_slot()
        raise


def guarded_poll_and_download(*args, **kwargs):
    """Luôn nhả lock kể cả khi timeout/lỗi để app không bị kẹt trạng thái bận."""
    try:
        return _raw_poll_and_download(*args, **kwargs)
    finally:
        release_generation_slot()
