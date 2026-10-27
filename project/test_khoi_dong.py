"""Test offline cho khoi_dong.py (D23): chọn Ollama nào, cờ ComfyUI theo VRAM, tắt đúng cái mình bật.

Không cần Ollama, ComfyUI hay GPU: Ollama giả là một HTTP server nhỏ chạy trong test."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import khoi_dong as kd


class _FakeOllama(BaseHTTPRequestHandler):
    models = ["qwen2.5:3b"]
    loaded = ["qwen2.5:3b"]
    unloaded: list = []

    def log_message(self, *args):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._send({"version": "test"})
        if self.path == "/api/tags":
            return self._send({"models": [{"name": n} for n in self.models]})
        if self.path == "/api/ps":
            return self._send({"models": [{"name": n} for n in self.loaded]})
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/generate" and req.get("keep_alive") == 0:
            type(self).unloaded.append(req["model"])
            return self._send({"done": True})
        self.send_error(404)


class FakeOllamaServer:
    def __enter__(self):
        _FakeOllama.unloaded = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def dead_url() -> str:
    """Một cổng chắc chắn không có ai nghe."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


class ChooseOllamaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "Ollama"

    def test_user_url_wins_and_nothing_is_started(self):
        choice = kd.choose_ollama({"OLLAMA_URL": "http://10.0.0.5:11434/"}, self.root)
        self.assertEqual(choice["url"], "http://10.0.0.5:11434")
        self.assertIsNone(choice["start"])
        self.assertIn("bạn đặt", choice["source"])

    def test_drive_ollama_already_running_is_used_as_is(self):
        with patch.object(kd, "ollama_up", lambda url: url == kd.DRIVE_OLLAMA_URL):
            choice = kd.choose_ollama({}, self.root)
        self.assertEqual(choice["url"], kd.DRIVE_OLLAMA_URL)
        self.assertIsNone(choice["start"])

    def test_drive_ollama_is_started_when_installed_on_the_drive(self):
        self.root.mkdir()
        (self.root / kd.EXE_NAME).write_bytes(b"")
        # Ollama cài trên máy đang chạy cũng KHÔNG được ưu tiên hơn ổ rời: model nằm ở ổ rời.
        with patch.object(kd, "ollama_up", lambda url: url == kd.INSTALLED_OLLAMA_URL):
            choice = kd.choose_ollama({}, self.root)
        self.assertEqual(choice["url"], kd.DRIVE_OLLAMA_URL)
        self.assertEqual(choice["start"], self.root)

    def test_installed_ollama_is_the_fallback(self):
        with patch.object(kd, "ollama_up", lambda url: url == kd.INSTALLED_OLLAMA_URL):
            choice = kd.choose_ollama({}, self.root)
        self.assertEqual(choice["url"], kd.INSTALLED_OLLAMA_URL)
        self.assertIsNone(choice["start"])

    def test_nothing_found(self):
        with patch.object(kd, "ollama_up", lambda url: False):
            choice = kd.choose_ollama({}, self.root)
        self.assertIsNone(choice["source"])

    def test_drive_port_is_not_paodich_port(self):
        # PaoDich bật Ollama riêng của nó ở 11434; chung cổng là thấy nhầm thư mục model.
        self.assertNotEqual(kd.DRIVE_OLLAMA_PORT, 11434)

    def test_drive_root_sits_on_the_project_drive(self):
        project = Path(tempfile.gettempdir()) / "x" / "project"
        self.assertEqual(kd.drive_ollama_root(project), Path(project.anchor) / "Ollama")


class ComfyArgsTest(unittest.TestCase):
    def test_small_or_unknown_vram_keeps_lowvram(self):
        for vram in (None, 4.0, 8.0, 9.9):
            self.assertIn("--lowvram", kd.comfy_args(vram), vram)

    def test_big_gpu_drops_lowvram(self):
        self.assertNotIn("--lowvram", kd.comfy_args(12.0))
        self.assertNotIn("--lowvram", kd.comfy_args(24.0))

    def test_never_opens_its_own_browser_tab(self):
        self.assertIn("--disable-auto-launch", kd.comfy_args(4.0))

    def test_vram_reads_largest_gpu_and_survives_missing_smi(self):
        fake = {"gpus": [{"total_mib": 4096}, {"total_mib": 24576}]}
        with patch("bench_baseline.query_gpus", return_value=fake):
            self.assertEqual(kd.vram_gib(), 24.0)
        with patch("bench_baseline.query_gpus", return_value={"gpus": []}):
            self.assertIsNone(kd.vram_gib())
        with patch("bench_baseline.query_gpus", side_effect=RuntimeError("driver treo")):
            self.assertIsNone(kd.vram_gib())


class HttpHelpersTest(unittest.TestCase):
    def test_up_models_and_unload_against_fake_ollama(self):
        with FakeOllamaServer() as fake:
            self.assertTrue(kd.ollama_up(fake.url))
            self.assertEqual(kd.ollama_models(fake.url), ["qwen2.5:3b"])
            kd.unload_ollama(fake.url)
            self.assertEqual(_FakeOllama.unloaded, ["qwen2.5:3b"])

    def test_dead_server_is_just_false(self):
        url = dead_url()
        self.assertFalse(kd.ollama_up(url))
        self.assertEqual(kd.ollama_models(url), [])
        self.assertFalse(kd.comfy_up(url))
        kd.unload_ollama(url)   # không ném lỗi

    def test_system_proxy_is_ignored_for_localhost(self):
        with FakeOllamaServer() as fake, patch.dict(os.environ, {"HTTP_PROXY": "http://10.255.255.1:9",
                                                               "http_proxy": "http://10.255.255.1:9"}):
            self.assertTrue(kd.ollama_up(fake.url))

    def test_has_model(self):
        self.assertTrue(kd.has_model(["qwen2.5:3b"], "qwen2.5:3b"))
        self.assertTrue(kd.has_model(["llama3:latest"], "llama3"))
        self.assertFalse(kd.has_model(["qwen3:4b-instruct"], "qwen2.5:3b"))


class StopTreeTest(unittest.TestCase):
    def test_stops_a_running_process_and_ignores_finished_ones(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        out = StringIO()
        with redirect_stdout(out):
            kd.stop_tree(proc, "thử")
        self.assertIsNotNone(proc.poll())
        self.assertIn("Đã tắt thử", out.getvalue())
        with redirect_stdout(StringIO()) as again:
            kd.stop_tree(proc, "thử")
            kd.stop_tree(None, "không có")
        self.assertEqual(again.getvalue(), "")


class MainTest(unittest.TestCase):
    def run_main(self, argv, env=None):
        out = StringIO()
        with patch.dict(os.environ, env or {}, clear=False), redirect_stdout(out):
            code = kd.main(argv)
        return code, out.getvalue()

    def test_check_only_reports_plan_and_starts_nothing(self):
        with FakeOllamaServer() as fake, \
                patch.object(kd, "app_up", lambda url, gradio: False), \
                patch.object(kd, "comfy_up", lambda url: False), \
                patch.object(kd, "vram_gib", lambda: 4.0), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("không được bật gì")):
            code, out = self.run_main(["--kiem-tra", "--model", "qwen3:4b-instruct"],
                                      {"OLLAMA_URL": fake.url})
        self.assertEqual(code, 0)
        self.assertIn("OLLAMA_URL bạn đặt", out)
        self.assertIn("Model  : qwen3:4b-instruct", out)
        self.assertIn("--lowvram", out)

    def test_strong_gpu_turns_off_low_vram_mode_unless_user_set_it(self):
        common = [patch.object(kd, "app_up", lambda url, gradio: False),
                  patch.object(kd, "comfy_up", lambda url: False),
                  patch.object(kd, "vram_gib", lambda: 24.0)]
        with FakeOllamaServer() as fake:
            for p in common:
                p.start()
            try:
                env = {"OLLAMA_URL": fake.url}
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("YUMEAI_LOW_VRAM_MODE", None)
                    code, out = self.run_main(["--kiem-tra"], env)
                self.assertIn("VRAM bình thường", out)
                self.assertIn("YUMEAI_LOW_VRAM_MODE=0", out)
                code, out = self.run_main(["--kiem-tra"], {**env, "YUMEAI_LOW_VRAM_MODE": "1"})
                self.assertNotIn("YUMEAI_LOW_VRAM_MODE=0", out)
            finally:
                for p in common:
                    p.stop()

    def test_missing_ollama_is_explained(self):
        with patch.object(kd, "choose_ollama", lambda env, root=None: {"url": kd.DRIVE_OLLAMA_URL,
                                                                       "source": None, "start": None}), \
                patch.object(kd, "app_up", lambda url, gradio: False), \
                patch.object(kd, "comfy_up", lambda url: True):
            code, out = self.run_main(["--kiem-tra"], {"OLLAMA_URL": ""})
        self.assertIn("CHUAN-BI-OLLAMA.cmd", out)

    def test_app_already_running_only_opens_browser(self):
        opened = []
        with patch.object(kd, "app_up", lambda url, gradio: True), \
                patch.object(kd.webbrowser, "open", opened.append), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("không được bật gì")):
            code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(opened, [kd.APP_URL + "/app"])
        self.assertIn("đã chạy sẵn", out)

    def test_started_ollama_is_stopped_after_the_app_exits(self):
        """Bật Ollama (giả) → chạy cửa vào (giả, thoát ngay với mã 3) → Ollama bị tắt, mã giữ nguyên."""
        started = []
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kwargs):
            if cmd[-1] == "serve":
                proc = real_popen([sys.executable, "-c", "import time; time.sleep(60)"])
                started.append(proc)
                return proc
            return real_popen([sys.executable, "-c", "import sys; sys.exit(3)"])

        tmp = Path(tempfile.mkdtemp())
        with FakeOllamaServer() as fake, \
                patch.object(kd, "DRIVE_OLLAMA_URL", fake.url), \
                patch.object(kd, "choose_ollama", lambda env, root=None: {"url": fake.url,
                                                                          "source": "ổ rời", "start": tmp}), \
                patch.object(kd, "app_up", lambda url, gradio: False), \
                patch.object(kd, "comfy_up", lambda url: True), \
                patch.object(subprocess, "Popen", fake_popen):
            code, out = self.run_main(["--khong-mo-trinh-duyet"])
        self.assertEqual(code, 3)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll(), "Ollama do file này bật phải bị tắt")
        self.assertIn("Đã tắt Ollama", out)
        self.assertEqual(_FakeOllama.unloaded, ["qwen2.5:3b"], "nhả model trước khi tắt")

    def test_missing_model_is_reported(self):
        with FakeOllamaServer() as fake, \
                patch.object(kd, "app_up", lambda url, gradio: False), \
                patch.object(kd, "comfy_up", lambda url: True), \
                patch.object(subprocess, "Popen", lambda cmd, **kw: _Done()):
            code, out = self.run_main(["--khong-mo-trinh-duyet", "--model", "gemma3:4b"], {"OLLAMA_URL": fake.url})
        self.assertIn("chưa có model gemma3:4b", out)
        self.assertIn("qwen2.5:3b", out)


class _Done:
    pid = 0

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


if __name__ == "__main__":
    unittest.main()
