"""
bench_baseline — ĐO NỀN giai đoạn 0 (D17) trên máy thật, trước khi thêm model/tính năng mới.

    python bench_baseline.py                  # 3 job text2img SD1.5, seed cố định
    python bench_baseline.py --runs 5         # nhiều job hơn → số warm ổn định hơn
    python bench_baseline.py --warm-ollama    # nạp model Ollama trước MỖI job để đo D16 thật
    python bench_baseline.py --check-only     # chỉ kiểm tra GPU/RAM/đĩa/dịch vụ, không vẽ

Đo gì:
  1. Môi trường: GPU + VRAM (nvidia-smi), RAM, chỗ trống trên ổ chứa ComfyUI/models (cần
     ≥ 30 GiB cho model của giai đoạn sau).
  2. ComfyUI và Ollama có đang chạy không — CHỈ HỎI, không bao giờ tự khởi động.
  3. N job text2img SD1.5 đi ĐÚNG đường mà POST /api/images/generate dùng:
     get_checkpoint_model → build_positive_prompt → build_quality_workflow (preset mặc định
     của REST) → runtime.run_comfy_job (Resource Guard + nhả VRAM Ollama theo D16).
     Job 1 là cold (ComfyUI phải nạp checkpoint), các job sau là warm. Để job 1 chắc chắn
     cold, bench gửi ComfyUI /free trước job 1 (nhả model + xoá cache TRONG BỘ NHỚ, không
     đụng file nào; tắt bằng --no-free). VRAM và RAM được lấy mẫu mỗi 0.5 giây bằng một luồng
     nền để có số đỉnh.
  4. D16: thời gian unload_ollama_models() bên trong từng job, và ngay trước job Ollama có
     đang giữ model nào trong VRAM không.

Không làm:
  - Không đổi biến môi trường/cấu hình app, không xoá file nào.
  - Không ghi DB, không lưu ảnh vào gallery (data/images). ComfyUI vẫn tự lưu PNG như mọi
    job; bench chỉ đổi filename_prefix để ảnh nằm riêng ở ComfyUI/output/yumeai_bench/.
  - Prompt CỐ ĐỊNH: phong cảnh không người, không dùng persona nào, để kết quả lặp lại được
    và không phụ thuộc hồ sơ nhân vật. Cố ý không có tham số --prompt.

Kết quả: <YUMEAI_DATA_DIR, mặc định project/data>/bench/pc0_baseline_<thời điểm>.json, cùng
một file _testlog.md chứa khối dán vào docs/TESTLOG.md (khối này cũng được in ra màn hình).

Mã thoát: 0 = đo xong; 1 = có job lỗi (phần đo được vẫn được ghi); 2 = không bắt đầu được
(ComfyUI tắt, đang bận, thiếu checkpoint...); 130 = dừng bằng Ctrl+C.

Các hàm dò máy (nvidia-smi, RAM, đĩa) hiện chỉ bench dùng nên nằm ở đây. Khi làm "hồ sơ máy"
của D17, chuyển chúng vào yumeai/ để app và bench dùng chung — đừng copy sang chỗ thứ hai.
"""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx

from yumeai.core import runtime
from yumeai.core.config import (
    COMFY_MODELS_DIR,
    COMFY_TIMEOUT_SEC,
    COMFY_URL,
    DATA_DIR,
    OLLAMA_MODEL,
    OLLAMA_NUM_GPU,
    OLLAMA_URL,
    VRAM_HANDOFF,
)
from yumeai.core.image_prompt import NEGATIVE_PROMPT, build_positive_prompt
from yumeai.services import comfy_service, ollama_service

BENCH_SCHEMA = "yumeai.bench.pc0/1"
PROJECT_DIR = Path(__file__).resolve().parent

DEFAULT_RUNS = 3
MAX_RUNS = 20
DEFAULT_SEED = 20261021
MAX_SEED = 2**31   # cùng khoảng seed với main.py (random.randint(1, 2**31))

# "Anime" = mặc định của ImgReq.style → bench nạp đúng checkpoint mà REST chọn khi người
# dùng không chỉ định style.
BENCH_STYLE = "Anime"
# Phong cảnh không người: không cần persona, không phụ thuộc hồ sơ nhân vật nào. "no humans"
# và "scenery" là tag mà checkpoint anime SD1.5 hiểu rõ nhất cho cảnh không có người.
BENCH_SUBJECT = (
    "no humans, scenery, mountain lake at sunrise, pine forest, "
    "morning mist over calm water, clear sky"
)
# Ảnh của bench nằm riêng trong ComfyUI/output/yumeai_bench/, không lẫn với ảnh dùng thật.
BENCH_FILENAME_PREFIX = "yumeai_bench/pc0"

SAMPLE_INTERVAL_S = 0.5
# Chờ ComfyUI xử lý cờ /free (nó xử lý ngay khi rảnh) và VRAM hạ xuống trước khi đo nền.
FREE_SETTLE_S = 3.0
HTTP_TIMEOUT_S = 5.0
NVIDIA_SMI_TIMEOUT_S = 5.0
NVIDIA_SMI_ARGS = (
    "--query-gpu=name,memory.total,memory.used",
    "--format=csv,noheader,nounits",
)
# Driver NVIDIA trên Windows đặt nvidia-smi ở System32 (bản mới) hoặc NVSMI (bản cũ);
# có máy không đưa thư mục đó vào PATH.
WINDOWS_NVIDIA_SMI_CANDIDATES = (
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
)

# Chỗ trống cần cho model của giai đoạn sau (ước lượng trong kế hoạch pipeline ảnh/video
# local 2026-10-21): Klein ~11 GiB + MOSS ~13 GiB, cộng ~6 GiB dư cho file tải dở/cache.
NEXT_PHASE_MODELS_GIB = {"Klein": 11, "MOSS": 13}
REQUIRED_FREE_GIB = 30.0

# Biến môi trường app đọc và có thể đổi kết quả đo. Chỉ GHI LẠI giá trị, không bao giờ đặt.
WATCHED_ENV = (
    "OLLAMA_URL", "OLLAMA_MODEL", "COMFY_URL", "COMFY_MODELS_DIR", "YUMEAI_DATA_DIR",
    "YUMEAI_TRANSLATE_MODEL", "YUMEAI_VRAM_HANDOFF", "YUMEAI_OLLAMA_NUM_GPU",
    "YUMEAI_COMFY_TIMEOUT_SEC", "YUMEAI_RESOURCE_GUARD", "YUMEAI_LOW_VRAM_MODE",
)

_MIB = 1024 ** 2
_GIB = 1024 ** 3


# ══════════════════════════════════════════════════════════════════════════
# GPU — nvidia-smi
# ══════════════════════════════════════════════════════════════════════════
_NA_VALUES = {"", "n/a", "[n/a]", "[not supported]", "[unknown error]"}


def _parse_mib(raw: str):
    """'4096' → 4096; '[N/A]'/rác → None (để bên gọi ghi rõ là không đọc được)."""
    value = raw.strip()
    if value.lower() in _NA_VALUES:
        return None
    try:
        return int(round(float(value)))
    except ValueError:
        return None


def parse_nvidia_smi(output: str) -> tuple[list[dict], list[str]]:
    """Đọc output `--query-gpu=name,memory.total,memory.used --format=csv,noheader,nounits`.

    Trả về (danh sách GPU, danh sách vấn đề). Dòng hỏng không làm mất các dòng còn lại,
    và luôn được ghi vào `problems` — bỏ qua im lặng thì số VRAM sai mà không ai biết.
    Tách từ PHẢI (rsplit) vì hai cột cuối luôn là số, còn tên GPU thì không chắc.
    """
    gpus: list[dict] = []
    problems: list[str] = []
    for line_no, line in enumerate((output or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            problems.append(f"dòng {line_no} không đúng dạng 'tên, tổng, đang dùng': {line[:80]!r}")
            continue
        name = parts[0].strip()
        total = _parse_mib(parts[1])
        used = _parse_mib(parts[2])
        if not name or total is None:
            problems.append(f"dòng {line_no} thiếu tên GPU hoặc tổng VRAM: {line[:80]!r}")
            continue
        if used is None:
            problems.append(
                f"dòng {line_no}: nvidia-smi không báo VRAM đang dùng ({parts[2].strip()!r})"
            )
        gpus.append({"name": name, "total_mib": total, "used_mib": used})
    return gpus, problems


def find_nvidia_smi(which=shutil.which, candidates=None, exists=os.path.isfile):
    """Đường dẫn nvidia-smi, hoặc None nếu máy không có."""
    found = which("nvidia-smi")
    if found:
        return found
    if candidates is None:
        candidates = WINDOWS_NVIDIA_SMI_CANDIDATES if sys.platform == "win32" else ()
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return None


def query_gpus(runner=None, smi_path=None) -> dict:
    """Hỏi nvidia-smi một lần. KHÔNG ném lỗi: thiếu/hỏng thì `available=False` kèm `note`
    nói rõ vì sao, để bench vẫn đo được thời gian trên máy không có GPU NVIDIA."""
    runner = runner or subprocess.run
    smi = smi_path or find_nvidia_smi()
    info = {"available": False, "smi": smi, "gpus": [], "problems": [], "note": ""}
    if not smi:
        info["note"] = (
            "Không tìm thấy nvidia-smi (chưa cài driver NVIDIA hoặc máy không có GPU NVIDIA) "
            "→ bỏ qua đo VRAM; thời gian vẫn đo bình thường."
        )
        return info
    try:
        proc = runner(
            [smi, *NVIDIA_SMI_ARGS], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=NVIDIA_SMI_TIMEOUT_S,
        )
    except FileNotFoundError:
        info["note"] = f"Không chạy được {smi} (file không tồn tại) → bỏ qua đo VRAM."
        return info
    except subprocess.TimeoutExpired:
        info["note"] = (
            f"nvidia-smi không trả lời sau {NVIDIA_SMI_TIMEOUT_S:.0f}s (driver treo?) "
            "→ bỏ qua đo VRAM."
        )
        return info
    except OSError as e:
        info["note"] = f"Lỗi khi chạy nvidia-smi: {e} → bỏ qua đo VRAM."
        return info
    if proc.returncode != 0:
        detail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
        info["note"] = (
            f"nvidia-smi thoát mã {proc.returncode}: {detail[0] if detail else '(không có thông báo)'} "
            "→ thường là driver NVIDIA chưa nạp; bỏ qua đo VRAM."
        )
        return info
    gpus, problems = parse_nvidia_smi(proc.stdout)
    info.update(gpus=gpus, problems=problems)
    if not gpus:
        info["note"] = "nvidia-smi chạy được nhưng không đọc được dòng GPU nào → bỏ qua đo VRAM."
        return info
    info["available"] = True
    if len(gpus) > 1:
        info["note"] = (
            f"Máy có {len(gpus)} GPU NVIDIA; bench đo GPU đầu tiên nvidia-smi liệt kê "
            f"({gpus[0]['name']})."
        )
    return info


def make_vram_probe(smi_path, runner=None):
    """Hàm lấy VRAM đang dùng (MiB) của GPU đầu tiên — cho luồng lấy mẫu."""
    def probe():
        info = query_gpus(runner=runner, smi_path=smi_path)
        if not info["available"]:
            raise RuntimeError(info["note"])
        used = info["gpus"][0]["used_mib"]
        if used is None:
            raise RuntimeError("nvidia-smi không báo VRAM đang dùng")
        return used
    return probe


# ══════════════════════════════════════════════════════════════════════════
# RAM — chỉ thư viện chuẩn (psutil không nằm trong requirements.txt)
# ══════════════════════════════════════════════════════════════════════════
def parse_meminfo(text: str) -> dict:
    """/proc/meminfo (kB) → {'total_mib', 'available_mib'}; thiếu trường thì None."""
    values = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[key.strip()] = int(fields[0])
    total, avail = values.get("MemTotal"), values.get("MemAvailable")
    return {
        "total_mib": round(total / 1024, 1) if total is not None else None,
        "available_mib": round(avail / 1024, 1) if avail is not None else None,
    }


def _read_ram_windows() -> dict:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError("GlobalMemoryStatusEx thất bại")
    return {
        "total_mib": round(stat.ullTotalPhys / _MIB, 1),
        "available_mib": round(stat.ullAvailPhys / _MIB, 1),
    }


def read_ram(platform_name=None) -> dict:
    """RAM tổng/còn trống (MiB). KHÔNG ném lỗi: hệ điều hành lạ thì trả None kèm `note`."""
    plat = platform_name or sys.platform
    info = {"total_mib": None, "available_mib": None, "source": None, "note": ""}
    try:
        if plat == "win32":
            info.update(_read_ram_windows(), source="GlobalMemoryStatusEx")
        elif plat.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii", errors="replace") as fh:
                info.update(parse_meminfo(fh.read()), source="/proc/meminfo")
        else:
            info["note"] = (
                f"Chưa hỗ trợ đọc RAM trên '{plat}' bằng thư viện chuẩn (psutil không phải "
                "dependency của dự án) → bỏ qua số RAM."
            )
    except (OSError, AttributeError, ValueError) as e:
        info["note"] = f"Không đọc được RAM: {e} → bỏ qua số RAM."
    return info


def _ram_available_probe():
    available = read_ram()["available_mib"]
    if available is None:
        raise RuntimeError("không đọc được RAM trống")
    return available


# ══════════════════════════════════════════════════════════════════════════
# ĐĨA — chỗ cho model của giai đoạn sau
# ══════════════════════════════════════════════════════════════════════════
def disk_verdict(free_bytes: int, required_gib: float = REQUIRED_FREE_GIB) -> dict:
    free_gib = free_bytes / _GIB
    return {
        "free_gib": round(free_gib, 2),
        "required_gib": required_gib,
        "enough": free_gib >= required_gib,
    }


def check_disk(models_dir: str = COMFY_MODELS_DIR, usage=shutil.disk_usage) -> dict:
    """Chỗ trống trên ổ THẬT chứa ComfyUI/models.

    realpath trước: models có thể là junction/symlink sang ổ khác, đo nhầm ổ chứa project là
    báo "đủ" trong khi model sẽ được tải vào ổ đã đầy. Thư mục chưa có thì đo thư mục cha
    gần nhất đang tồn tại (model sẽ được tạo ở đó).
    """
    target = os.path.realpath(models_dir)
    probe = target
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    info = {"models_dir": models_dir, "resolved": target, "checked_path": probe,
            "models_dir_exists": os.path.isdir(target)}
    try:
        u = usage(probe)
    except OSError as e:
        info.update(error=f"Không đo được chỗ trống của {probe}: {e}", enough=None,
                    free_gib=None, total_gib=None, required_gib=REQUIRED_FREE_GIB)
        return info
    info.update(disk_verdict(u.free), total_gib=round(u.total / _GIB, 2))
    return info


# ══════════════════════════════════════════════════════════════════════════
# DỊCH VỤ — chỉ hỏi, không khởi động
# ══════════════════════════════════════════════════════════════════════════
def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


def probe_comfy(url: str = COMFY_URL, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """GET /system_stats: có chạy không, phiên bản, argv (có --lowvram không), thiết bị."""
    info = {"url": url, "up": False}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/system_stats")
            if r.status_code != 200:
                info["error"] = f"HTTP {r.status_code} từ /system_stats"
                return info
            stats = r.json()
    except Exception as e:
        info["error"] = _err(e)
        return info
    system = stats.get("system") or {}
    argv = [str(a) for a in (system.get("argv") or [])]
    devices = []
    for d in stats.get("devices") or []:
        devices.append({
            "name": d.get("name"),
            "type": d.get("type"),
            "vram_total_mib": round((d.get("vram_total") or 0) / _MIB, 1),
            "vram_free_mib": round((d.get("vram_free") or 0) / _MIB, 1),
            "torch_vram_free_mib": round((d.get("torch_vram_free") or 0) / _MIB, 1),
        })
    info.update(
        up=True,
        version=system.get("comfyui_version"),
        pytorch=system.get("pytorch_version"),
        argv=argv,
        lowvram="--lowvram" in argv,
        devices=devices,
    )
    return info


def comfy_queue_state(url: str = COMFY_URL, timeout: float = HTTP_TIMEOUT_S):
    """(số job đang chạy, số job đang chờ) trong ComfyUI, hoặc None nếu không hỏi được."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/queue")
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    return len(data.get("queue_running") or []), len(data.get("queue_pending") or [])


def ollama_loaded_models(url: str = OLLAMA_URL, timeout: float = HTTP_TIMEOUT_S):
    """Model Ollama đang nạp (GET /api/ps), kèm phần nằm trong VRAM. None = không hỏi được."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/api/ps")
            r.raise_for_status()
            models = r.json().get("models") or []
    except Exception:
        return None
    return [
        {
            "name": m.get("model") or m.get("name") or "?",
            "size_mib": round((m.get("size") or 0) / _MIB, 1),
            "vram_mib": round((m.get("size_vram") or 0) / _MIB, 1),
        }
        for m in models
    ]


def probe_ollama(url: str = OLLAMA_URL, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """GET /api/tags giống /health của main.py, rồi /api/ps để biết model nào đang chiếm VRAM."""
    info = {"url": url, "up": False}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/api/tags")
            if r.status_code != 200:
                info["error"] = f"HTTP {r.status_code} từ /api/tags"
                return info
    except Exception as e:
        info["error"] = _err(e)
        return info
    info["up"] = True
    info["loaded"] = ollama_loaded_models(url, timeout)
    return info


def request_comfy_free(url: str = COMFY_URL, timeout: float = HTTP_TIMEOUT_S) -> tuple[bool, str]:
    """POST /free unload_models + free_memory để job 1 chắc chắn là cold.

    Không dùng comfy_service.free_comfy_vram(): hàm đó dành cho bước vision nên tự bỏ qua
    khi YUMEAI_VRAM_HANDOFF=0 hoặc YUMEAI_OLLAMA_NUM_GPU=0 — ở đây cần nhả trong MỌI cấu
    hình. ComfyUI 0.22 mặc định cache kết quả node theo áp lực RAM, nên không xoá cache thì
    lần chạy bench thứ hai với cùng seed có thể "xong" ngay mà không vẽ gì.
    """
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{url}/free", json={"unload_models": True, "free_memory": True})
    except Exception as e:
        return False, _err(e)
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code} từ /free"
    return True, ""


def fetch_history(prompt_id: str, url: str = COMFY_URL, timeout: float = 10.0):
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/history/{prompt_id}")
            r.raise_for_status()
            return r.json().get(prompt_id)
    except Exception:
        return None


def parse_history_entry(entry) -> dict:
    """Thời gian ComfyUI thật sự chạy (execution_start → execution_success, ms) và các node
    lấy từ cache. Thời gian wall của run_comfy_job có độ trễ poll tới 2 giây; số này thì không."""
    out = {"comfy_status": None, "exec_s": None, "cached_nodes": None}
    if not isinstance(entry, dict):
        return out
    status = entry.get("status") or {}
    out["comfy_status"] = status.get("status_str")
    stamps = {}
    for msg in status.get("messages") or []:
        if not (isinstance(msg, (list, tuple)) and len(msg) == 2 and isinstance(msg[1], dict)):
            continue
        event, data = msg
        if event == "execution_cached":
            out["cached_nodes"] = [str(n) for n in data.get("nodes") or []]
        if isinstance(data.get("timestamp"), (int, float)):
            stamps[event] = data["timestamp"]
    if "execution_start" in stamps and "execution_success" in stamps:
        out["exec_s"] = round((stamps["execution_success"] - stamps["execution_start"]) / 1000, 2)
    return out


# ══════════════════════════════════════════════════════════════════════════
# LẤY MẪU VRAM/RAM TRONG LUỒNG NỀN
# ══════════════════════════════════════════════════════════════════════════
class ResourceSampler:
    """Lấy mẫu VRAM đang dùng + RAM trống mỗi `interval` giây trong luồng nền.

    Probe hỏng không làm hỏng job: lỗi được ĐẾM và giữ thông báo đầu tiên trong summary,
    không nuốt im lặng. mark("unload_done") tách đỉnh VRAM sau khi Ollama đã nhả (D16) —
    với --warm-ollama, đỉnh cả cửa sổ có thể là của Ollama chứ không phải của ComfyUI.
    """

    def __init__(self, vram_probe=None, ram_probe=None, interval=None):
        self.vram_probe = vram_probe
        self.ram_probe = ram_probe
        # Đọc hằng số lúc tạo, không lúc định nghĩa hàm: test cần đặt chu kỳ ngắn hơn.
        self.interval = SAMPLE_INTERVAL_S if interval is None else interval
        self.samples: list[tuple[float, object, object]] = []
        self.marks: dict[str, float] = {}
        self.errors = 0
        self.first_error = None
        self._stop = threading.Event()
        self._thread = None
        self._t0 = time.perf_counter()

    def _read(self, probe):
        if probe is None:
            return None
        try:
            return probe()
        except Exception as e:
            self.errors += 1
            if self.first_error is None:
                self.first_error = str(e)
            return None

    def _sample(self):
        t = time.perf_counter() - self._t0
        self.samples.append((t, self._read(self.vram_probe), self._read(self.ram_probe)))

    def _loop(self):
        next_tick = time.perf_counter()
        while True:
            self._sample()
            next_tick += self.interval
            if self._stop.wait(max(0.0, next_tick - time.perf_counter())):
                return

    def start(self):
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, name="bench-sampler", daemon=True)
        self._thread.start()
        return self

    def mark(self, name: str):
        self.marks[name] = time.perf_counter() - self._t0

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            # nvidia-smi treo thì luồng có thể kẹt tới hết timeout của nó; không chờ vô hạn.
            self._thread.join(timeout=self.interval + NVIDIA_SMI_TIMEOUT_S + 1)
        self._sample()   # một mẫu cuối, ngay khi job vừa xong
        return self.summary()

    def summary(self) -> dict:
        vram = [(t, v) for t, v, _ in self.samples if v is not None]
        ram = [r for _, _, r in self.samples if r is not None]
        mark = self.marks.get("unload_done")
        after = [v for t, v in vram if mark is not None and t >= mark]
        return {
            "samples": len(self.samples),
            "interval_s": self.interval,
            "vram_first_mib": vram[0][1] if vram else None,
            "vram_peak_mib": max(v for _, v in vram) if vram else None,
            "vram_peak_after_unload_mib": max(after) if after else None,
            "ram_min_available_mib": min(ram) if ram else None,
            "probe_errors": self.errors,
            "first_error": self.first_error,
            "vram_series": [[round(t, 2), v] for t, v in vram],
        }


# ══════════════════════════════════════════════════════════════════════════
# WORKLOAD — đúng đường REST, chỉ khác seed cố định và chỗ lưu ảnh
# ══════════════════════════════════════════════════════════════════════════
def retarget_save_nodes(workflow: dict, prefix: str = BENCH_FILENAME_PREFIX) -> int:
    """Đổi filename_prefix của mọi node SaveImage. Không thấy node nào thì DỪNG: nghĩa là
    build_quality_workflow đã đổi cấu trúc và bench không còn đo đúng thứ nó tưởng."""
    count = 0
    for node in workflow.values():
        if node.get("class_type") == "SaveImage":
            node["inputs"]["filename_prefix"] = prefix
            count += 1
    if not count:
        raise RuntimeError(
            "Workflow không có node SaveImage nào — build_quality_workflow đã đổi cấu trúc? "
            "Sửa bench_baseline.py cho khớp trước khi đo."
        )
    return count


def _node_id(workflow: dict, class_type: str):
    return next((nid for nid, n in workflow.items() if n.get("class_type") == class_type), None)


def build_workloads(model: str, seeds: list[int], style: str = BENCH_STYLE):
    """Prompt + workflow cho từng seed, dựng bằng ĐÚNG các hàm REST dùng (D6)."""
    # appearance="" có chủ đích: bench không dùng persona nào.
    prompt = build_positive_prompt(BENCH_SUBJECT, style)
    workflows = []
    for seed in seeds:
        wf = comfy_service.build_quality_workflow(
            model, prompt, NEGATIVE_PROMPT, seed,
            quality=comfy_service.QUALITY_BALANCED, upscale=comfy_service.UPSCALE_NONE,
        )
        retarget_save_nodes(wf)
        workflows.append(wf)
    return prompt, workflows


def describe_workload(model: str, prompt: str, workflow: dict, seeds: list[int]) -> dict:
    """Đọc thông số từ CHÍNH workflow sẽ gửi đi, không chép lại hằng số (lệch là sai số đo)."""
    sampler_id = _node_id(workflow, "KSampler")
    latent_id = _node_id(workflow, "EmptyLatentImage")
    ks = workflow.get(sampler_id, {}).get("inputs", {})
    latent = workflow.get(latent_id, {}).get("inputs", {})
    return {
        "path": "get_checkpoint_model → build_positive_prompt → build_quality_workflow → runtime.run_comfy_job",
        "style": BENCH_STYLE,
        "checkpoint": model,
        "prompt": prompt,
        "negative_prompt": NEGATIVE_PROMPT,
        "quality": comfy_service.QUALITY_BALANCED,
        "upscale": comfy_service.UPSCALE_NONE,
        "steps": ks.get("steps"),
        "cfg": ks.get("cfg"),
        "sampler": ks.get("sampler_name"),
        "scheduler": ks.get("scheduler"),
        "width": latent.get("width"),
        "height": latent.get("height"),
        "seeds": list(seeds),
        "filename_prefix": BENCH_FILENAME_PREFIX,
        "checkpoint_node": _node_id(workflow, "CheckpointLoaderSimple"),
        "sampler_node": sampler_id,
    }


@contextmanager
def instrument_runtime(record: dict, sampler=None):
    """Bọc TẠM hai điểm bên trong runtime.run_comfy_job để đo, không sửa code dùng chung.

    - unload_ollama_models: đo thời gian bước nhả VRAM Ollama (D16) đúng như REST chạy nó.
    - _raw_submit_workflow: lấy prompt_id để đọc /history (thời gian ComfyUI + node cache).
    Luôn trả lại hàm gốc khi ra khỏi khối, kể cả khi job lỗi.
    """
    for name in ("unload_ollama_models", "_raw_submit_workflow"):
        if not callable(getattr(runtime, name, None)):
            raise RuntimeError(
                f"yumeai/core/runtime.py không còn `{name}` — cập nhật bench_baseline.py theo."
            )
    orig_unload = runtime.unload_ollama_models
    orig_submit = runtime._raw_submit_workflow

    def timed_unload(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            result = orig_unload(*args, **kwargs)
        finally:
            record["unload_s"] = round(time.perf_counter() - t0, 3)
            if sampler is not None:
                sampler.mark("unload_done")
        record["unloaded"] = list(result or [])
        return result

    def capture_submit(workflow, *args, **kwargs):
        prompt_id = orig_submit(workflow, *args, **kwargs)
        record["prompt_id"] = prompt_id
        return prompt_id

    runtime.unload_ollama_models = timed_unload
    runtime._raw_submit_workflow = capture_submit
    try:
        yield record
    finally:
        runtime.unload_ollama_models = orig_unload
        runtime._raw_submit_workflow = orig_submit


def run_one_job(index: int, total: int, workflow: dict, seed: int, *, timeout: int,
                warm_ollama: bool, sampler_factory, workload: dict) -> dict:
    """Một job text2img qua runtime.run_comfy_job. Không bao giờ ném lỗi ra ngoài: lỗi được
    ghi vào job["error"] để bench còn ghi được phần đã đo."""
    job = {"index": index, "role": "cold" if index == 1 else "warm", "seed": seed, "ok": False}
    if warm_ollama:
        t0 = time.perf_counter()
        ollama_service.warmup_ollama()
        job["ollama_warmup_s"] = round(time.perf_counter() - t0, 2)
    job["ollama_before"] = ollama_loaded_models()
    sampler = sampler_factory()
    sampler.start()
    t0 = time.perf_counter()
    try:
        with instrument_runtime(job, sampler):
            img = runtime.run_comfy_job(
                workflow, None, 0.0, timeout, label=f"bench_baseline job {index}/{total}",
            )
        job["wall_s"] = round(time.perf_counter() - t0, 2)
        job["ok"] = True
        # Băm điểm ảnh để so giữa các lần chạy: cùng seed + cùng máy mà khác hash là ComfyUI
        # không tất định — đáng biết trước khi so chất lượng model mới.
        job["image"] = {
            "size": list(img.size),
            "sha256_16": hashlib.sha256(img.tobytes()).hexdigest()[:16],
        }
    except KeyboardInterrupt:
        job["wall_s"] = round(time.perf_counter() - t0, 2)
        job["error"] = ("Dừng bằng Ctrl+C. Job đã gửi vẫn chạy tiếp trong ComfyUI cho tới khi "
                        "xong — bench không tự huỷ nó.")
        job["interrupted"] = True
    except Exception as e:
        job["wall_s"] = round(time.perf_counter() - t0, 2)
        job["error"] = str(e) if isinstance(
            e, (comfy_service.ComfyUIError, runtime.ResourceBusyError)) else _err(e)
    finally:
        job["resources"] = sampler.stop()
    if job.get("prompt_id"):
        job.update(parse_history_entry(fetch_history(job["prompt_id"])))
    cached = job.get("cached_nodes")
    job["checkpoint_cached"] = None if cached is None else workload["checkpoint_node"] in cached
    # KSampler lấy từ cache = ComfyUI không vẽ gì cả; số đo của job đó vô nghĩa.
    job["sampler_cached"] = None if cached is None else workload["sampler_node"] in cached
    return job


# ══════════════════════════════════════════════════════════════════════════
# TỔNG HỢP
# ══════════════════════════════════════════════════════════════════════════
def time_stats(values) -> dict | None:
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def _peak(jobs, key="vram_peak_after_unload_mib"):
    """Đỉnh VRAM của ComfyUI: ưu tiên phần sau khi Ollama đã nhả, không có thì cả cửa sổ."""
    peaks = []
    for job in jobs:
        res = job.get("resources") or {}
        value = res.get(key)
        if value is None:
            value = res.get("vram_peak_mib")
        if value is not None:
            peaks.append(value)
    return max(peaks) if peaks else None


def _ram_min(jobs):
    values = [(j.get("resources") or {}).get("ram_min_available_mib") for j in jobs]
    values = [v for v in values if v is not None]
    return min(values) if values else None


def summarize_d16(jobs: list[dict], handoff_enabled: bool) -> dict:
    before = [j.get("ollama_before") for j in jobs]
    reachable = any(b is not None for b in before)
    vram_before = [m["vram_mib"] for b in before if b for m in b]
    return {
        "enabled": handoff_enabled,
        "ollama_reachable": reachable,
        "had_models_in_vram": any(v > 0 for v in vram_before),
        "vram_before_max_mib": max(vram_before) if vram_before else 0.0,
        "unloaded_models": sorted({m for j in jobs for m in j.get("unloaded") or []}),
        "unload_s": time_stats(j.get("unload_s") for j in jobs),
    }


def summarize_jobs(jobs: list[dict], handoff_enabled: bool) -> dict:
    valid = [j for j in jobs if j["ok"] and not j.get("sampler_cached")]
    cold = next((j for j in valid if j["index"] == 1), None)
    warm = [j for j in valid if j["index"] > 1]
    summary = {
        "jobs_total": len(jobs),
        "jobs_ok": sum(1 for j in jobs if j["ok"]),
        "jobs_invalid_cached": [j["index"] for j in jobs if j["ok"] and j.get("sampler_cached")],
        "cold": None,
        "warm": None,
        "d16": summarize_d16(jobs, handoff_enabled),
    }
    if cold:
        summary["cold"] = {
            "wall_s": cold.get("wall_s"),
            "exec_s": cold.get("exec_s"),
            # True = /history xác nhận checkpoint KHÔNG lấy từ cache; None = không kiểm được.
            "verified_cold": None if cold.get("checkpoint_cached") is None
            else not cold["checkpoint_cached"],
            "vram_peak_mib": _peak([cold]),
            "ram_min_available_mib": _ram_min([cold]),
        }
    if warm:
        summary["warm"] = {
            "wall_s": time_stats(j.get("wall_s") for j in warm),
            "exec_s": time_stats(j.get("exec_s") for j in warm),
            "vram_peak_mib": _peak(warm),
            "ram_min_available_mib": _ram_min(warm),
        }
    return summary


def watched_env(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    return {k: environ[k] for k in WATCHED_ENV if k in environ}


def collect_environment() -> dict:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "gpu": query_gpus(),
        "ram": read_ram(),
        "disk": check_disk(),
    }


def build_result(*, started_at, command, args, environment, services, workload, free_info,
                 jobs, files) -> dict:
    return {
        "schema": BENCH_SCHEMA,
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": command,
        "args": args,
        "environment": environment,
        "services": services,
        "config": {
            "VRAM_HANDOFF": VRAM_HANDOFF,
            "OLLAMA_NUM_GPU": OLLAMA_NUM_GPU,
            "OLLAMA_MODEL": OLLAMA_MODEL,
            "COMFY_TIMEOUT_SEC": COMFY_TIMEOUT_SEC,
            "RESOURCE_GUARD": runtime.YUMEAI_RESOURCE_GUARD,
            "LOW_VRAM_MODE": runtime.YUMEAI_LOW_VRAM_MODE,
            "env": watched_env(),
        },
        "workload": workload,
        "free_before_first_job": free_info,
        "jobs": jobs,
        "summary": summarize_jobs(jobs, VRAM_HANDOFF),
        "notes": [
            "wall_s = thời gian runtime.run_comfy_job như REST thấy: gồm D16, gửi job, poll "
            "(mỗi 2 giây) và tải ảnh về.",
            "exec_s = ComfyUI execution_start → execution_success trong /history, không có độ trễ poll.",
            "Ảnh không vào DB/gallery; ComfyUI tự lưu PNG vào output/yumeai_bench/.",
        ],
        "files": files,
    }


# ══════════════════════════════════════════════════════════════════════════
# GHI KẾT QUẢ + KHỐI TESTLOG
# ══════════════════════════════════════════════════════════════════════════
def _fmt_s(value) -> str:
    return f"{value:.1f}s" if isinstance(value, (int, float)) else "—"


def _fmt_gib(mib) -> str:
    return f"{mib / 1024:.1f} GiB" if isinstance(mib, (int, float)) else "không đọc được"


def _fmt_mib(value, total=None) -> str:
    if not isinstance(value, (int, float)):
        return "không đo được"
    text = f"{value:.0f}"
    return f"{text}/{total:.0f} MiB" if isinstance(total, (int, float)) else f"{text} MiB"


def _cell(text) -> str:
    """Giá trị đi vào ô bảng markdown: không được có '|' hay xuống dòng."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _first_gpu(result: dict):
    gpus = (result["environment"].get("gpu") or {}).get("gpus") or []
    return gpus[0] if gpus else None


def _d16_row(result: dict) -> tuple[str, str, str]:
    d16 = result["summary"]["d16"]
    times = d16.get("unload_s")
    when = f"TB {times['mean']:.2f}s (max {times['max']:.2f}s)" if times else "—"
    if not d16["enabled"]:
        return "tắt (YUMEAI_VRAM_HANDOFF=0)", when, "không đo được D16 khi tắt"
    if not d16["ollama_reachable"]:
        return "Ollama không chạy", when, "không có gì để nhả; bật `ollama serve` rồi đo lại"
    if d16["unloaded_models"]:
        return ("đã nhả " + ", ".join(d16["unloaded_models"]), when,
                f"trước job Ollama giữ tới {d16['vram_before_max_mib']:.0f} MiB VRAM")
    return "không có gì để nhả", when, "Ollama không giữ model nào trong VRAM trước job"


def format_testlog_block(result: dict) -> str:
    """Khối markdown theo mẫu của docs/TESTLOG.md, chỉ gồm các hạng mục bench THẬT SỰ đo."""
    env, svc, wl, summ = (result["environment"], result["services"], result["workload"],
                          result["summary"])
    gpu = _first_gpu(result)
    ram = env.get("ram") or {}
    disk = env.get("disk") or {}
    comfy = svc.get("comfyui") or {}
    total_mib = gpu["total_mib"] if gpu else None
    free_info = result.get("free_before_first_job") or {}
    lines = []

    machine = f"{gpu['name']} {gpu['total_mib']} MiB" if gpu else "GPU không đọc được"
    lines.append(f"## {result['started_at'][:10]} — {machine}, RAM {_fmt_gib(ram.get('total_mib'))}"
                 " — đo nền giai đoạn 0")
    lines.append("")
    lines.append("**Phần cứng**")
    lines.append(f"- GPU: {gpu['name'] if gpu else (env.get('gpu') or {}).get('note', '—')}")
    if gpu:
        lines.append(f"- VRAM: {gpu['total_mib']} MiB (đang dùng {_fmt_mib(gpu['used_mib'])} lúc bắt đầu)")
    else:
        lines.append("- VRAM: không đo được")
    lines.append(f"- RAM: {_fmt_gib(ram.get('total_mib'))} (trống {_fmt_gib(ram.get('available_mib'))} lúc bắt đầu)")
    lines.append(f"- HĐH: {env.get('platform')}; Python {env.get('python')}")
    lines.append("")
    lines.append("**Cấu hình chạy**")
    lines.append(f"- OLLAMA_MODEL: {result['config']['OLLAMA_MODEL']}")
    lines.append(f"- Model ComfyUI (checkpoint / Kontext quant): {wl.get('checkpoint')} (SD1.5) / không dùng")
    env_set = result["config"].get("env") or {}
    changed = ", ".join(f"{k}={v}" for k, v in env_set.items()) or "không có (mặc định)"
    lines.append(f"- Biến môi trường đã đổi: {changed} — bench không tự đặt biến nào")
    argv = " ".join(comfy.get("argv") or []) or "?"
    lines.append(f"- ComfyUI: v{comfy.get('version') or '?'}, PyTorch {comfy.get('pytorch') or '?'}, `{argv}`")
    seeds = wl.get("seeds") or []
    if free_info.get("ok"):
        free_text = "có"
    elif free_info.get("requested"):
        free_text = f"gửi lỗi ({free_info.get('error')})"
    else:
        free_text = "không"
    seed_text = f"{seeds[0]}–{seeds[-1]}" if len(seeds) > 1 else (str(seeds[0]) if seeds else "—")
    lines.append(
        f"- Đo: {len(seeds)} job text2img qua `run_comfy_job`, {wl.get('quality')} "
        f"{wl.get('width')}x{wl.get('height')}, {wl.get('sampler')}/{wl.get('scheduler')} "
        f"cfg {wl.get('cfg')}, seed {seed_text}, prompt phong cảnh cố định; "
        f"/free trước job 1: {free_text}; "
        f"--warm-ollama: {'có' if result['args'].get('warm_ollama') else 'không'}"
    )
    lines.append("")
    lines.append("**Kết quả**")
    lines.append("| Hạng mục | Kết quả | Thời gian | Ghi chú |")
    lines.append("|---|---|---|---|")

    jobs = result["jobs"]
    job1 = next((j for j in jobs if j["index"] == 1), None)
    cold = summ.get("cold")
    if cold:
        verdict = {True: "pass", None: "pass (chưa xác nhận cold)",
                   False: "pass (KHÔNG cold)"}[cold["verified_cold"]]
        note = (f"ComfyUI chạy {_fmt_s(cold['exec_s'])}; VRAM đỉnh "
                f"{_fmt_mib(cold['vram_peak_mib'], total_mib)}; RAM trống thấp nhất "
                f"{_fmt_gib(cold['ram_min_available_mib'])}")
        lines.append(f"| text2img SD1.5 — job 1 (cold, nạp checkpoint) | {verdict} | "
                     f"{_fmt_s(cold['wall_s'])} | {_cell(note)} |")
    elif job1 is not None:
        lines.append(f"| text2img SD1.5 — job 1 (cold, nạp checkpoint) | fail | "
                     f"{_fmt_s(job1.get('wall_s'))} | xem Lỗi gặp phải |")
    warm = summ.get("warm")
    if warm:
        w = warm["wall_s"]
        e = warm["exec_s"]
        when = f"TB {w['mean']:.1f}s ({w['min']:.1f}–{w['max']:.1f}s)"
        note = (f"ComfyUI chạy TB {_fmt_s(e['mean'] if e else None)}; VRAM đỉnh "
                f"{_fmt_mib(warm['vram_peak_mib'], total_mib)}; RAM trống thấp nhất "
                f"{_fmt_gib(warm['ram_min_available_mib'])}")
        lines.append(f"| text2img SD1.5 — warm ({w['n']} job) | pass | {when} | {_cell(note)} |")
    elif len(seeds) == 1:
        lines.append("| text2img SD1.5 — warm | chưa đo | — | chạy với --runs 1 |")
    else:
        lines.append("| text2img SD1.5 — warm | fail | — | không job warm nào hợp lệ, xem Lỗi gặp phải |")
    d16_result, d16_time, d16_note = _d16_row(result)
    lines.append(f"| D16 — nhả VRAM Ollama trước job | {_cell(d16_result)} | {d16_time} | {_cell(d16_note)} |")
    if free_info.get("ok"):
        lines.append(f"| VRAM lúc nghỉ (sau /free, trước job 1) | "
                     f"{_fmt_mib(free_info.get('vram_used_after_mib'), total_mib)} | — | "
                     "phần còn lại sau khi ComfyUI nhả model: CUDA context + Windows/ứng dụng khác |")
    if disk.get("enough") is None:
        lines.append(f"| Chỗ trống cho model giai đoạn sau | không đo được | — | {_cell(disk.get('error', ''))} |")
    else:
        need = " + ".join(f"{k} ~{v}" for k, v in NEXT_PHASE_MODELS_GIB.items())
        lines.append(
            f"| Chỗ trống cho model giai đoạn sau | {'đủ' if disk['enough'] else 'THIẾU'} | — | "
            f"{_cell(disk.get('checked_path'))} còn {disk['free_gib']:.1f} GiB; cần ≥ "
            f"{disk['required_gib']:.0f} GiB ({need} GiB + dư) |"
        )
    lines.append("")
    lines.append("Thời gian = wall time của `run_comfy_job` như REST thấy (gồm D16, poll 2 giây, "
                 "tải ảnh); \"ComfyUI chạy\" = execution_start → execution_success trong `/history`.")
    lines.append("")
    lines.append("**Lỗi gặp phải**")
    failed = [j for j in jobs if j.get("error")]
    for j in failed:
        lines.append(f"- Job {j['index']} (seed {j['seed']}): {' / '.join(str(j['error']).splitlines())}")
    if summ.get("jobs_invalid_cached"):
        invalid = ", ".join(str(i) for i in summ["jobs_invalid_cached"])
        lines.append(f"- Job {invalid}: KSampler lấy từ cache ComfyUI → không tính.")
    if not failed and not summ.get("jobs_invalid_cached"):
        lines.append("- Không có.")
    lines.append("")
    lines.append("**Kết luận / cần đổi gì trong code**")
    if disk.get("enough") is False:
        lines.append(f"- Ổ chứa ComfyUI/models chỉ còn {disk['free_gib']:.1f} GiB < "
                     f"{disk['required_gib']:.0f} GiB: dọn chỗ trước khi tải model giai đoạn sau.")
    if comfy.get("up") and not comfy.get("lowvram"):
        lines.append("- ComfyUI KHÔNG chạy `--lowvram`: số đo khác cấu hình chuẩn của RUNBOOK.")
    if cold and cold["verified_cold"] is False:
        lines.append("- Job 1 không phải cold (checkpoint đã nằm trong cache ComfyUI): khởi động "
                     "lại ComfyUI hoặc bỏ --no-free rồi đo lại.")
    d16 = summ["d16"]
    if d16["enabled"] and d16["ollama_reachable"] and not d16["unloaded_models"]:
        lines.append("- D16 chưa có gì để nhả trong lượt này; chạy thêm "
                     "`python bench_baseline.py --warm-ollama` để đo giá nhả VRAM thật.")
    files = result.get("files") or {}
    lines.append(f"- Số đo tự động: `python {' '.join(result['command'])}`; dữ liệu đầy đủ: "
                 f"`{files.get('json_display', files.get('json', '?'))}`.")
    lines.append("- (điền kết luận sau khi xem số đo)")
    return "\n".join(lines) + "\n"


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(path)


def plan_output_paths(out_dir: Path, stamp: str) -> tuple[Path, Path]:
    """Tên file chưa tồn tại — bench KHÔNG BAO GIỜ ghi đè kết quả cũ."""
    for n in range(1, 1000):
        suffix = "" if n == 1 else f"-{n}"
        json_path = out_dir / f"pc0_baseline_{stamp}{suffix}.json"
        md_path = out_dir / f"pc0_baseline_{stamp}{suffix}_testlog.md"
        if not json_path.exists() and not md_path.exists():
            return json_path, md_path
    raise RuntimeError(f"Quá nhiều file trùng thời điểm {stamp} trong {out_dir}")


def _write_new(path: Path, text: str) -> None:
    # "x" = tạo mới, có file trùng thì lỗi thay vì ghi đè. newline="\n" để file giống nhau
    # trên Windows/Linux (docs dùng LF, xem AGENTS.md mục 6).
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_results(result: dict, json_path: Path, md_path: Path, testlog: str) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    _write_new(json_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    _write_new(md_path, testlog)


# ══════════════════════════════════════════════════════════════════════════
# IN RA MÀN HÌNH
# ══════════════════════════════════════════════════════════════════════════
def _section(title: str) -> None:
    print(f"\n{'═' * 62}\n{title}\n{'═' * 62}")


def print_environment(env: dict, services: dict) -> None:
    _section("1. Môi trường")
    gpu = env["gpu"]
    if gpu["available"]:
        for g in gpu["gpus"]:
            print(f"  GPU    : {g['name']} — tổng {g['total_mib']} MiB, đang dùng {_fmt_mib(g['used_mib'])}")
    else:
        print(f"  GPU    : ⚠️ {gpu['note']}")
    for problem in gpu["problems"]:
        print(f"           ⚠️ {problem}")
    if gpu["available"] and gpu["note"]:
        print(f"           ℹ️ {gpu['note']}")
    ram = env["ram"]
    if ram["total_mib"] is not None:
        print(f"  RAM    : tổng {_fmt_gib(ram['total_mib'])}, còn trống {_fmt_gib(ram['available_mib'])}")
    else:
        print(f"  RAM    : ⚠️ {ram['note']}")
    disk = env["disk"]
    if disk.get("enough") is None:
        print(f"  Đĩa    : ⚠️ {disk.get('error')}")
    else:
        print(f"  Đĩa    : {disk['checked_path']} — trống {disk['free_gib']:.1f} / {disk['total_gib']:.1f} GiB")
        need = " + ".join(f"{k} ~{v} GiB" for k, v in NEXT_PHASE_MODELS_GIB.items())
        if disk["enough"]:
            print(f"           ✅ ĐỦ ≥ {disk['required_gib']:.0f} GiB cho model giai đoạn sau ({need} + dư)")
        else:
            print(f"           ❌ THIẾU: cần ≥ {disk['required_gib']:.0f} GiB ({need} + dư) — "
                  "dọn chỗ trước khi tải model mới. Đo nền SD1.5 vẫn chạy được.")
        if not disk["models_dir_exists"]:
            print(f"           ⚠️ Chưa có thư mục {disk['models_dir']} — đã đo ổ của thư mục cha.")
    print(f"  Python : {env['python']} ({env['python_executable']}) — {env['platform']}")

    _section("2. Dịch vụ (chỉ hỏi, không tự khởi động)")
    comfy = services["comfyui"]
    if comfy["up"]:
        argv = " ".join(comfy.get("argv") or [])
        print(f"  ComfyUI: 🟢 {comfy['url']} — v{comfy.get('version') or '?'}, "
              f"PyTorch {comfy.get('pytorch') or '?'}")
        print(f"           argv: {argv}")
        if not comfy.get("lowvram"):
            print("           ⚠️ KHÔNG có --lowvram — RUNBOOK yêu cầu cờ này trên GPU 4GB; số đo sẽ "
                  "khác cấu hình chuẩn.")
    else:
        print(f"  ComfyUI: 🔴 {comfy['url']} — {comfy.get('error')}")
    ollama = services["ollama"]
    if ollama["up"]:
        loaded = ollama.get("loaded")
        if loaded is None:
            text = "không đọc được /api/ps"
        elif loaded:
            text = ", ".join(f"{m['name']} (VRAM {m['vram_mib']:.0f} MiB)" for m in loaded)
        else:
            text = "chưa nạp model nào"
        print(f"  Ollama : 🟢 {ollama['url']} — {text}")
    else:
        print(f"  Ollama : 🔴 {ollama['url']} — {ollama.get('error')}")
        print("           (không bắt buộc cho bench: chỉ D16 là không có gì để đo; bật bằng `ollama serve`)")


def print_job(job: dict, total: int, total_vram) -> None:
    res = job.get("resources") or {}
    head = f"  Job {job['index']}/{total} ({job['role']}) seed {job['seed']}:"
    if not job["ok"]:
        print(f"{head} ❌ sau {_fmt_s(job.get('wall_s'))}\n     {job.get('error')}")
        return
    peak = res.get("vram_peak_after_unload_mib")
    if peak is None:
        peak = res.get("vram_peak_mib")
    unloaded = ", ".join(job.get("unloaded") or []) or "không có gì để nhả"
    unload_s = job.get("unload_s")
    d16 = f"{unload_s:.2f}s" if isinstance(unload_s, (int, float)) else "không đo được"
    print(f"{head} ✅ {_fmt_s(job.get('wall_s'))} (ComfyUI {_fmt_s(job.get('exec_s'))}) · "
          f"VRAM đỉnh {_fmt_mib(peak, total_vram)} · D16 {d16} ({unloaded})")
    if job["role"] == "cold" and job.get("checkpoint_cached"):
        print("     ⚠️ Job 1 KHÔNG cold: checkpoint lấy từ cache ComfyUI (đã dùng --no-free?).")
    if job.get("sampler_cached"):
        print("     ⚠️ KSampler lấy từ cache ComfyUI — job này không vẽ gì, không tính vào số đo.")
    if res.get("probe_errors"):
        print(f"     ⚠️ {res['probe_errors']} lần lấy mẫu VRAM/RAM lỗi: {res.get('first_error')}")


# ══════════════════════════════════════════════════════════════════════════
# CHƯƠNG TRÌNH CHÍNH
# ══════════════════════════════════════════════════════════════════════════
def _int_in_range(lo: int, hi: int, what: str):
    def convert(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{what} phải là số nguyên, nhận được {raw!r}") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"{what} phải trong khoảng {lo}–{hi}, nhận được {value}")
        return value
    return convert


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench_baseline.py",
        description="Đo nền giai đoạn 0 (D17): môi trường + text2img SD1.5 cold/warm + D16. "
                    "Không khởi động dịch vụ, không đổi cấu hình, không ghi DB/gallery.",
    )
    parser.add_argument("--runs", type=_int_in_range(1, MAX_RUNS, "--runs"), default=DEFAULT_RUNS,
                        help=f"số job text2img (mặc định {DEFAULT_RUNS}; job 1 = cold, còn lại = warm)")
    parser.add_argument("--seed", type=_int_in_range(0, MAX_SEED - MAX_RUNS, "--seed"),
                        default=DEFAULT_SEED,
                        help=f"seed của job 1; job i dùng seed+i-1 (mặc định {DEFAULT_SEED})")
    parser.add_argument("--timeout", type=_int_in_range(30, 7200, "--timeout"),
                        default=COMFY_TIMEOUT_SEC,
                        help=f"giây chờ tối đa mỗi job (mặc định = YUMEAI_COMFY_TIMEOUT_SEC = {COMFY_TIMEOUT_SEC})")
    parser.add_argument("--out-dir", default=os.path.join(DATA_DIR, "bench"),
                        help="thư mục ghi kết quả (mặc định <YUMEAI_DATA_DIR>/bench)")
    parser.add_argument("--warm-ollama", action="store_true",
                        help="nạp model Ollama (warmup_ollama) trước MỖI job, giống REST "
                             "enrich rồi vẽ, để đo giá nhả VRAM của D16")
    parser.add_argument("--no-free", action="store_true",
                        help="KHÔNG gửi /free trước job 1 (khi đó job 1 có thể không cold)")
    parser.add_argument("--check-only", action="store_true",
                        help="chỉ kiểm tra GPU/RAM/đĩa/ComfyUI/Ollama, không chạy job nào")
    return parser.parse_args(argv)


def _prefer_utf8_stdout() -> None:
    """Chuyển hướng ra file trên Windows thì stdout dùng mã trang cũ (cp125x) và chết ở ký tự
    tiếng Việt đầu tiên. Ép UTF-8 để `> bench.txt` vẫn chạy và đọc lại được."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def free_before_first_job(smi_path) -> dict:
    ok, err = request_comfy_free()
    info = {"requested": True, "ok": ok, "error": err or None, "vram_used_after_mib": None}
    if not ok:
        print(f"  ⚠️ Không gửi được /free cho ComfyUI ({err}) — job 1 có thể không cold.")
        return info
    time.sleep(FREE_SETTLE_S)
    if smi_path:
        gpu = query_gpus(smi_path=smi_path)
        if gpu["available"]:
            info["vram_used_after_mib"] = gpu["gpus"][0]["used_mib"]
    print(f"  🧹 Đã nhờ ComfyUI nhả model + xoá cache (/free) → job 1 sẽ nạp checkpoint từ đầu. "
          f"VRAM lúc nghỉ: {_fmt_mib(info['vram_used_after_mib'])}")
    return info


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _prefer_utf8_stdout()   # trước argparse: cả --help cũng có tiếng Việt
    args = parse_args(argv)
    started = datetime.now().astimezone()
    print(f"{'═' * 62}\nĐO NỀN GIAI ĐOẠN 0 — YumeAI (D17) · {started:%Y-%m-%d %H:%M:%S}\n{'═' * 62}")

    environment = collect_environment()
    services = {"comfyui": probe_comfy(), "ollama": probe_ollama()}
    print_environment(environment, services)
    if args.check_only:
        print("\n(--check-only: không chạy job nào, không ghi file)")
        return 0

    if not services["comfyui"]["up"]:
        print("\n" + comfy_service.COMFY_DOWN_MSG.format(
            url=COMFY_URL, err=services["comfyui"].get("error")))
        return 2
    queue = comfy_queue_state()
    if queue is None:
        print("  ⚠️ Không đọc được hàng đợi ComfyUI (/queue) — không kiểm được có job lạ chen vào.")
    elif queue[0] or queue[1]:
        print(f"\n❌ ComfyUI đang có {queue[0]} job chạy và {queue[1]} job chờ (có thể từ giao diện "
              "web của ComfyUI). Đo lúc này là đo lẫn job khác. Đợi hàng đợi trống rồi chạy lại.")
        return 2

    _section("3. Đo text2img SD1.5 (đường REST, không persona)")
    try:
        model = comfy_service.get_checkpoint_model(BENCH_STYLE)
    except comfy_service.ComfyUIError as e:
        print(f"\n❌ {e}")
        return 2
    seeds = [args.seed + i for i in range(args.runs)]
    prompt, workflows = build_workloads(model, seeds)
    workload = describe_workload(model, prompt, workflows[0], seeds)
    print(f"  Checkpoint: {model} · {workload['quality']} · {workload['width']}x{workload['height']} · "
          f"seed {seeds[0]}{'–' + str(seeds[-1]) if len(seeds) > 1 else ''}")
    print(f"  Prompt    : {prompt}")

    gpu = environment["gpu"]
    smi_path = gpu["smi"] if gpu["available"] else None
    total_vram = gpu["gpus"][0]["total_mib"] if gpu["available"] else None

    def sampler_factory():
        return ResourceSampler(
            vram_probe=make_vram_probe(smi_path) if smi_path else None,
            ram_probe=_ram_available_probe if environment["ram"]["total_mib"] is not None else None,
        )

    jobs: list[dict] = []
    free_info = {"requested": False, "ok": False, "error": None, "vram_used_after_mib": None}
    interrupted = False
    try:
        # Giữ slot Resource Guard cho CẢ loạt job: tiến trình YumeAI khác (app_gradio.py,
        # main.py) không chen job vào giữa làm sai số cold/warm. run_comfy_job bên trong vẫn
        # đi qua guard (slot giữ lại được trong cùng luồng) và vẫn chạy D16 như REST.
        with runtime.comfy_job("bench_baseline (đo nền giai đoạn 0)"):
            if not args.no_free:
                free_info = free_before_first_job(smi_path)
            for index, (seed, workflow) in enumerate(zip(seeds, workflows), 1):
                job = run_one_job(index, len(seeds), workflow, seed, timeout=args.timeout,
                                  warm_ollama=args.warm_ollama, sampler_factory=sampler_factory,
                                  workload=workload)
                jobs.append(job)
                print_job(job, len(seeds), total_vram)
                if not job["ok"]:
                    break
    except KeyboardInterrupt:
        # Ctrl+C ngoài job (lúc chờ /free hoặc lúc nạp model Ollama cho --warm-ollama): vẫn
        # ghi phần đã đo (nếu có) thay vì văng traceback. Ctrl+C TRONG job do run_one_job bắt.
        print("\n⚠️ Dừng bằng Ctrl+C.")
        interrupted = True
    except runtime.ResourceBusyError as e:
        print(f"\n❌ {e}\n   Bench cần GPU một mình: đợi job đang chạy xong (hoặc đóng app_gradio.py/"
              "main.py đang vẽ) rồi chạy lại.")
        return 2

    if interrupted and not jobs:
        print("   Chưa có job nào xong → không ghi file kết quả.")
        return 130

    out_dir = Path(args.out_dir)
    json_path, md_path = plan_output_paths(out_dir, started.strftime("%Y%m%d-%H%M%S"))
    files = {"json": str(json_path), "testlog": str(md_path), "json_display": _display_path(json_path)}
    result = build_result(
        started_at=started.isoformat(timespec="seconds"),
        command=["bench_baseline.py", *argv],
        args=vars(args),
        environment=environment,
        services=services,
        workload=workload,
        free_info=free_info,
        jobs=jobs,
        files=files,
    )
    testlog = format_testlog_block(result)
    try:
        write_results(result, json_path, md_path, testlog)
    except OSError as e:
        print(f"\n❌ Không ghi được kết quả vào {out_dir}: {e}\n   Kiểm tra quyền ghi/ổ đĩa, hoặc "
              "chỉ thư mục khác bằng --out-dir. Khối TESTLOG vẫn in ra dưới đây.")
    else:
        print(f"\n💾 Đã ghi {_display_path(json_path)}\n   và {_display_path(md_path)}")

    _section("4. Khối dán vào docs/TESTLOG.md (ngay dưới dòng '---' đầu tiên — mục mới nhất ở trên)")
    print(testlog)

    if interrupted or any(j.get("interrupted") for j in jobs):
        return 130
    if not jobs or not all(j["ok"] for j in jobs):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
