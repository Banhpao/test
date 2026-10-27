"""Mở YumeAI bằng một cú bấm: bật Ollama, ComfyUI, rồi main.py và mở /app (D23).

Gọi từ ..\\MO-PAOCAKE.cmd, hoặc tay:
    cd project
    .venv\\Scripts\\python.exe khoi_dong.py                 # main.py + mở http://127.0.0.1:8000/app
    .venv\\Scripts\\python.exe khoi_dong.py --gradio        # app_gradio.py (cổng 7860) thay cho main.py
    .venv\\Scripts\\python.exe khoi_dong.py --model qwen3:4b-instruct
    .venv\\Scripts\\python.exe khoi_dong.py --kiem-tra      # chỉ in sẽ dùng gì, không bật gì

Ollama, theo thứ tự:
  1. OLLAMA_URL do bạn tự đặt → dùng đúng địa chỉ đó, không bật gì.
  2. Ollama trên ổ rời: <ổ chứa dự án>\\Ollama\\ollama.exe, cổng 11435, model ở <ổ>\\Ollama\\models.
     Đang chạy thì dùng luôn; chưa chạy thì bật (cửa sổ ẩn), và tắt khi bạn đóng app.
     Cổng riêng vì PaoDich bật Ollama của nó ở 11434 với thư mục model khác — chung cổng thì
     app này nói chuyện với Ollama của PaoDich và không thấy model của mình.
     Chuẩn bị (một lần cho cả ổ): E:\\duan\\_vscode\\CHUAN-BI-OLLAMA.cmd.
  3. Ollama cài trên máy (cổng 11434) nếu đang chạy.
ComfyUI: đang chạy ở COMFY_URL thì dùng luôn; chưa thì bật project\\ComfyUI\\main.py trong một
  cửa sổ riêng (thu nhỏ, xem được log). GPU dưới 10 GB VRAM (hoặc không đọc được) → --lowvram như
  RUNBOOK; từ 12 GB → bỏ --lowvram và YUMEAI_LOW_VRAM_MODE=0 (bỏ cap steps) trừ khi bạn đã tự đặt.
  --khong-comfy để chỉ chat.
Đóng: Ctrl+C trong cửa sổ này (hoặc đóng nó). Ollama/ComfyUI do chính file này bật được tắt
  theo; cái đang chạy sẵn từ trước thì để nguyên.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
DRIVE_OLLAMA_PORT = 11435          # trùng E:\duan\_vscode\ollama_o_roi.py
DRIVE_OLLAMA_URL = f"http://127.0.0.1:{DRIVE_OLLAMA_PORT}"
INSTALLED_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
APP_URL = "http://127.0.0.1:8000"
GRADIO_URL = "http://127.0.0.1:7860"
OLLAMA_START_TIMEOUT_S = 90
APP_READY_TIMEOUT_S = 180
LOWVRAM_BELOW_GIB = 10             # 4 GB (RTX 3050 Laptop) cần --lowvram; 8 GB vẫn chật cho Flux
STRONG_GPU_GIB = 12
EXE_NAME = "ollama.exe" if os.name == "nt" else "ollama"

# Không đi qua proxy hệ thống: máy có đặt proxy thì urllib gửi cả 127.0.0.1 sang proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def say(text: str = "") -> None:
    print(text, flush=True)


# ══════════════════════════════════════════════════════════════════════════
# HTTP tối thiểu (chỉ thư viện chuẩn: chạy được trước khi biết môi trường có gì)
# ══════════════════════════════════════════════════════════════════════════
def get_json(url: str, timeout: float = 2.0):
    try:
        with _LOCAL.open(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def post_json(url: str, payload: dict, timeout: float = 30.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with _LOCAL.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def ollama_up(url: str) -> bool:
    return isinstance(get_json(url.rstrip("/") + "/api/version"), dict)


def ollama_models(url: str) -> list[str]:
    doc = get_json(url.rstrip("/") + "/api/tags", timeout=5)
    if not isinstance(doc, dict):
        return []
    return [m.get("name") or m.get("model") for m in doc.get("models", []) if isinstance(m, dict)]


def comfy_up(url: str) -> bool:
    return isinstance(get_json(url.rstrip("/") + "/system_stats"), dict)


def app_up(url: str, gradio: bool) -> bool:
    if gradio:
        try:
            with _LOCAL.open(url, timeout=2):
                return True
        except (OSError, urllib.error.URLError):
            return False
    return isinstance(get_json(url + "/health"), dict)


def has_model(models: list[str], wanted: str) -> bool:
    return wanted in models or (":" not in wanted and f"{wanted}:latest" in models)


# ══════════════════════════════════════════════════════════════════════════
# Tiến trình
# ══════════════════════════════════════════════════════════════════════════
def drive_ollama_root(project: Path = PROJECT) -> Path:
    return Path(project.anchor) / "Ollama"


def start_hidden(cmd: list[str], *, cwd: Path, env: dict, log_path: Path) -> subprocess.Popen:
    log = open(log_path, "wb")
    try:
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        log.close()


def start_console(cmd: list[str], *, cwd: Path, env: dict) -> subprocess.Popen:
    """Cửa sổ riêng, thu nhỏ, không chiếm focus — để xem log ComfyUI khi cần."""
    kwargs = {}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 7     # SW_SHOWMINNOACTIVE
        kwargs = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": info}
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)


def on_console_close(callback) -> None:
    """Bấm X đóng cửa sổ thì Windows cho tiến trình ~5 giây rồi giết hẳn, khối finally không kịp
    chạy và Ollama/ComfyUI đã bật bị bỏ lại giữ VRAM. Bắt sự kiện đóng cửa sổ để tắt chúng trước."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event):
            if event in (2, 5, 6):      # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                callback()
                return True
            return False                # Ctrl+C / Ctrl+Break: để Python xử lý như thường

        on_console_close._keep = handler_type(handler)   # giữ tham chiếu, không thì bị thu gom
        ctypes.windll.kernel32.SetConsoleCtrlHandler(on_console_close._keep, True)
    except Exception:  # noqa: BLE001 — không bắt được thì chỉ mất phần dọn khi bấm X
        pass


def stop_tree(proc: subprocess.Popen | None, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    say(f"  Đã tắt {label} (do file này bật).")


def unload_ollama(url: str) -> None:
    """Nhả model trước khi tắt Ollama: tiến trình chạy model thoát êm và trả VRAM ngay."""
    doc = get_json(url + "/api/ps", timeout=5)
    for m in (doc or {}).get("models", []) if isinstance(doc, dict) else []:
        name = m.get("name") or m.get("model")
        if name:
            post_json(url + "/api/generate", {"model": name, "keep_alive": 0})


# ══════════════════════════════════════════════════════════════════════════
# Quyết định dùng gì
# ══════════════════════════════════════════════════════════════════════════
def choose_ollama(env: dict, root: Path | None = None) -> dict:
    """{'url', 'source', 'start'}: dùng Ollama nào, có phải bật không. Chưa bật gì.

    source None nghĩa là không tìm thấy Ollama nào."""
    if env.get("OLLAMA_URL"):
        url = env["OLLAMA_URL"].rstrip("/")
        return {"url": url, "source": "OLLAMA_URL bạn đặt", "start": None}
    root = root or drive_ollama_root()
    if ollama_up(DRIVE_OLLAMA_URL):
        return {"url": DRIVE_OLLAMA_URL, "source": f"Ollama ổ rời ({root}), đang chạy sẵn", "start": None}
    if (root / EXE_NAME).is_file():
        return {"url": DRIVE_OLLAMA_URL, "source": f"Ollama ổ rời ({root})", "start": root}
    if ollama_up(INSTALLED_OLLAMA_URL):
        return {"url": INSTALLED_OLLAMA_URL, "source": "Ollama cài trên máy (cổng 11434)", "start": None}
    return {"url": DRIVE_OLLAMA_URL, "source": None, "start": None}


def vram_gib() -> float | None:
    """VRAM lớn nhất trong các GPU NVIDIA (GiB), None nếu không đọc được."""
    try:
        from bench_baseline import query_gpus
        info = query_gpus()
    except Exception:  # noqa: BLE001 — không đọc được GPU thì coi như máy yếu, không làm hỏng việc mở app
        return None
    totals = [g.get("total_mib") for g in info.get("gpus", []) if g.get("total_mib")]
    return round(max(totals) / 1024, 1) if totals else None


def comfy_args(vram: float | None) -> list[str]:
    args = ["main.py", "--disable-auto-launch"]
    if vram is None or vram < LOWVRAM_BELOW_GIB:
        args.append("--lowvram")
    return args


def default_chat_model() -> str:
    """Model chat mặc định — đúng một chỗ định nghĩa: yumeai/core/config.py (D24)."""
    from yumeai.core.config import OLLAMA_MODEL
    return OLLAMA_MODEL


def comfy_dir(env: dict, project: Path = PROJECT) -> Path:
    return Path(env.get("COMFYUI_DIR") or project / "ComfyUI")


# ══════════════════════════════════════════════════════════════════════════
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mở YumeAI: Ollama + ComfyUI + main.py, rồi mở /app.")
    parser.add_argument("--gradio", action="store_true", help="mở app_gradio.py (cổng 7860) thay cho main.py")
    parser.add_argument("--model", help="model chat Ollama (mặc định: OLLAMA_MODEL, không đặt thì như yumeai/core/config.py)")
    parser.add_argument("--khong-comfy", action="store_true", help="không bật ComfyUI (chỉ chat)")
    parser.add_argument("--khong-mo-trinh-duyet", action="store_true", help="không tự mở trình duyệt")
    parser.add_argument("--kiem-tra", action="store_true", help="chỉ in sẽ dùng gì rồi thoát, không bật gì")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    env = dict(os.environ)
    if args.model:
        env["OLLAMA_MODEL"] = args.model
    model = env.get("OLLAMA_MODEL") or default_chat_model()
    gradio = args.gradio
    url_app = GRADIO_URL if gradio else APP_URL
    started: list[tuple[subprocess.Popen, str, str | None]] = []   # (tiến trình, tên, url Ollama để nhả)

    say("=== YumeAI — mở app")
    if app_up(APP_URL if not gradio else GRADIO_URL, gradio):
        say(f"  App đã chạy sẵn ở {url_app} — mở trình duyệt thôi.")
        if not args.kiem_tra and not args.khong_mo_trinh_duyet:
            webbrowser.open(url_app + ("" if gradio else "/app"))
        return 0

    # ── Ollama
    choice = choose_ollama(env)
    env["OLLAMA_URL"] = choice["url"]
    env["OLLAMA_MODEL"] = model
    if choice["source"] is None:
        say("  [THIẾU] Không thấy Ollama: chưa có Ollama trên ổ rời, máy cũng không có Ollama đang chạy.")
        say("          Chuẩn bị một lần cho cả ổ: bấm đúp E:\\duan\\_vscode\\CHUAN-BI-OLLAMA.cmd.")
        say("          App vẫn mở, nhưng chưa chat được cho tới khi có Ollama.")
    else:
        say(f"  Ollama : {choice['source']} — {choice['url']}")
    say(f"  Model  : {model}")

    # ── ComfyUI
    cdir = comfy_dir(env)
    comfy_url = (env.get("COMFY_URL") or DEFAULT_COMFY_URL).rstrip("/")
    comfy_plan = None
    if args.khong_comfy:
        say("  ComfyUI: không bật (--khong-comfy) — chỉ chat, không vẽ được.")
    elif comfy_up(comfy_url):
        say(f"  ComfyUI: đang chạy sẵn ở {comfy_url}")
    elif not (cdir / "main.py").is_file():
        say(f"  [THIẾU] ComfyUI: không có {cdir / 'main.py'} — app mở được nhưng không vẽ được.")
    else:
        vram = vram_gib()
        comfy_plan = comfy_args(vram)
        if vram is not None and vram >= STRONG_GPU_GIB and "YUMEAI_LOW_VRAM_MODE" not in env:
            env["YUMEAI_LOW_VRAM_MODE"] = "0"
        gpu = f"GPU {vram} GB VRAM" if vram is not None else "không đọc được VRAM"
        mode = "--lowvram" if "--lowvram" in comfy_plan else "VRAM bình thường"
        say(f"  ComfyUI: sẽ bật {cdir} ({gpu} → {mode}"
            f"{', YUMEAI_LOW_VRAM_MODE=0' if env.get('YUMEAI_LOW_VRAM_MODE') == '0' else ''})")
    entry = "app_gradio.py" if gradio else "main.py"
    say(f"  App    : {entry} → {url_app}{'' if gradio else '/app'}")
    if args.kiem_tra:
        return 0

    main_proc = None

    def kill_started():
        for proc, _label, _url in started:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)

    on_console_close(kill_started)
    try:
        if choice["start"] is not None:
            root = choice["start"]
            log_path = Path(tempfile.gettempdir()) / "paocake-ollama-o-roi.log"
            serve_env = {**env, "OLLAMA_HOST": f"127.0.0.1:{DRIVE_OLLAMA_PORT}",
                         "OLLAMA_MODELS": str(root / "models")}
            proc = start_hidden([str(root / EXE_NAME), "serve"], cwd=root, env=serve_env, log_path=log_path)
            started.append((proc, "Ollama", choice["url"]))
            deadline = time.monotonic() + OLLAMA_START_TIMEOUT_S
            while not ollama_up(choice["url"]):
                if proc.poll() is not None or time.monotonic() > deadline:
                    say(f"  [LỖI] Ollama không bật được — xem {log_path}. App vẫn mở, chưa chat được.")
                    break
                time.sleep(0.5)
            else:
                say(f"  Ollama : đã bật (log: {log_path})")
        if choice["source"] is not None and ollama_up(choice["url"]):
            models = ollama_models(choice["url"])
            if not has_model(models, model):
                say(f"  [THIẾU] Ollama này chưa có model {model}. Đang có: {', '.join(models) or 'không có'}.")
                say("          Chọn model khác: MO-PAOCAKE.cmd --model <tên>, hoặc chạy lại CHUAN-BI-OLLAMA.cmd.")
        if comfy_plan:
            proc = start_console([sys.executable, *comfy_plan], cwd=cdir, env=env)
            started.append((proc, "ComfyUI", None))
            say("  ComfyUI: đang khởi động trong cửa sổ thu nhỏ (mất khoảng 30–60 giây).")

        if not args.khong_mo_trinh_duyet:
            def open_when_ready():
                deadline = time.monotonic() + APP_READY_TIMEOUT_S
                while time.monotonic() < deadline:
                    if app_up(url_app, gradio):
                        webbrowser.open(url_app + ("" if gradio else "/app"))
                        return
                    time.sleep(1)
            threading.Thread(target=open_when_ready, daemon=True).start()

        say(f"\n  Đóng app: Ctrl+C trong cửa sổ này.\n{'-' * 60}")
        main_proc = subprocess.Popen([sys.executable, entry], cwd=str(PROJECT), env=env)
        return main_proc.wait()
    except KeyboardInterrupt:
        if main_proc is not None:
            try:
                main_proc.wait(timeout=20)       # Ctrl+C tới cả main.py: để nó tự dừng êm
            except subprocess.TimeoutExpired:
                main_proc.kill()
        return 130
    finally:
        if started:
            say("\n=== Đóng")
        for proc, label, url in reversed(started):
            if url:
                unload_ollama(url)
            stop_tree(proc, label)


if __name__ == "__main__":
    sys.exit(main())
