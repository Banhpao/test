"""Test offline cho bench_baseline.py (đo nền giai đoạn 0, D17).

Không cần ComfyUI, Ollama, nvidia-smi hay GPU: cả ba đều là bản giả. Chạy được trên máy
chưa có venv (chỉ thư viện chuẩn) — thiếu `httpx`/`Pillow` thì cắm module giả, giống
test_chat_context.py; bench không đụng tới phần thật của hai thư viện đó khi HTTP đã giả.

Phần quan trọng nhất là ca chạy trọn `main()`: job phải đi qua runtime.run_comfy_job THẬT
(Resource Guard + D16), workflow gửi đi phải đúng là của build_quality_workflow, không có
persona, không ghi DB/gallery, không đổi biến môi trường.
"""

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _install_fake_deps():
    """Máy không có venv: cắm httpx/PIL giả chỉ đủ để import được comfy_service."""
    if importlib.util.find_spec("httpx") is None:
        fake = types.ModuleType("httpx")
        fake.Client = None   # luôn bị thay bằng _FakeClient trong test
        fake.RequestError = type("RequestError", (Exception,), {})
        fake.HTTPError = type("HTTPError", (Exception,), {})
        sys.modules["httpx"] = fake
    if importlib.util.find_spec("PIL") is None:
        pil = types.ModuleType("PIL")
        image = types.ModuleType("PIL.Image")
        image.Image = type("Image", (), {})   # comfy_service dùng nó trong chữ ký hàm
        pil.Image = image
        sys.modules["PIL"] = pil
        sys.modules["PIL.Image"] = image


_install_fake_deps()

import httpx  # noqa: E402

import bench_baseline as bench  # noqa: E402
from yumeai.core import image_prompt, runtime  # noqa: E402
from yumeai.core.personas import PERSONAS  # noqa: E402
from yumeai.services import comfy_service, ollama_service  # noqa: E402

GIB = 1024 ** 3
SMI_LINE = "NVIDIA GeForce RTX 3050 Laptop GPU, 4096, {used}"


# ══════════════════════════════════════════════════════════════════════════
# Dịch vụ giả
# ══════════════════════════════════════════════════════════════════════════
class _FakeResp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data if data is not None else {}, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.Client giả: ghi lại mọi request, trả lời theo bảng `routes` (khớp đuôi URL).
    Giá trị là Exception thì ném ra — giả dịch vụ đang tắt."""
    calls = []
    routes = {}
    events = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self, method, url, json=None):
        _FakeClient.calls.append((method, url, json))
        if _FakeClient.events is not None:
            _FakeClient.events.append((method, url))
        for suffix, resp in _FakeClient.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp(json) if callable(resp) else resp
        return _FakeResp({}, 404)

    def get(self, url, **kwargs):
        return self._answer("GET", url)

    def post(self, url, json=None, **kwargs):
        return self._answer("POST", url, json)


DOWN = ConnectionRefusedError("[Errno 111] Connection refused")


def _history(pid, start_ms, end_ms, cached=()):
    return _FakeResp({pid: {"status": {"status_str": "success", "completed": True, "messages": [
        ["execution_start", {"prompt_id": pid, "timestamp": start_ms}],
        ["execution_cached", {"nodes": list(cached), "prompt_id": pid, "timestamp": start_ms + 1}],
        ["execution_success", {"prompt_id": pid, "timestamp": end_ms}],
    ]}, "outputs": {}}})


def comfy_routes(*, queue_running=0, argv=("main.py", "--lowvram"), sampler_cached_job=None):
    routes = {
        "/system_stats": _FakeResp({
            "system": {"comfyui_version": "0.22.0", "pytorch_version": "2.7.1+cu128",
                       "argv": list(argv)},
            "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 3050 Laptop GPU", "type": "cuda",
                         "index": 0, "vram_total": 4 * GIB, "vram_free": 3 * GIB,
                         "torch_vram_total": 0, "torch_vram_free": 0}],
        }),
        "/queue": _FakeResp({"queue_running": [["x"]] * queue_running, "queue_pending": []}),
        "/free": _FakeResp({}),
        # Job 1: ComfyUI phải nạp checkpoint (không node nào lấy từ cache) — 45.1 giây.
        "/history/pid-1": _history("pid-1", 1_000, 46_100),
    }
    for n in range(2, 6):
        cached = ["1", "2", "3"] + (["5"] if n == sampler_cached_job else [])
        routes[f"/history/pid-{n}"] = _history(f"pid-{n}", 0, 16_000, cached)
    return routes


def ollama_routes(*, in_vram=True):
    models = [{"name": "qwen2.5:3b", "model": "qwen2.5:3b", "size": int(2.3 * GIB),
               "size_vram": int(2.0 * GIB) if in_vram else 0}]
    return {
        "/api/tags": _FakeResp({"models": [{"name": "qwen2.5:3b"}]}),
        "/api/ps": _FakeResp({"models": models}),
        "/api/generate": _FakeResp({"done_reason": "unload"}),
    }


class _FakeSmi:
    """subprocess.run giả cho nvidia-smi; VRAM 'đang dùng' đổi theo trạng thái job giả."""

    def __init__(self):
        self.used = 300
        self.calls = 0

    def __call__(self, args, **kwargs):
        self.calls += 1
        return SimpleNamespace(returncode=0, stdout=SMI_LINE.format(used=self.used) + "\r\n",
                               stderr="")


class _FakeImage:
    size = (512, 768)

    def tobytes(self):
        return b"\x00" * 16


# ══════════════════════════════════════════════════════════════════════════
# 1. nvidia-smi
# ══════════════════════════════════════════════════════════════════════════
class NvidiaSmiParseTests(unittest.TestCase):
    def test_single_gpu_windows_line_endings(self):
        gpus, problems = bench.parse_nvidia_smi("NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 312\r\n")
        self.assertEqual(gpus, [{"name": "NVIDIA GeForce RTX 3050 Laptop GPU",
                                 "total_mib": 4096, "used_mib": 312}])
        self.assertEqual(problems, [])

    def test_multi_gpu_and_float_values(self):
        gpus, _ = bench.parse_nvidia_smi("GPU A, 8192, 100\nGPU B, 4096.0, 2048.4\n")
        self.assertEqual([g["name"] for g in gpus], ["GPU A", "GPU B"])
        self.assertEqual((gpus[1]["total_mib"], gpus[1]["used_mib"]), (4096, 2048))

    def test_comma_in_name_is_kept(self):
        """Tách từ phải: tên có dấu phẩy không làm lệch hai cột số."""
        gpus, _ = bench.parse_nvidia_smi("Vendor, Model X, 4096, 10")
        self.assertEqual(gpus[0]["name"], "Vendor, Model X")

    def test_not_available_used_is_reported_not_hidden(self):
        gpus, problems = bench.parse_nvidia_smi("RTX 3050, 4096, [N/A]")
        self.assertEqual(gpus[0]["used_mib"], None)
        self.assertEqual(len(problems), 1)
        self.assertIn("không báo VRAM đang dùng", problems[0])

    def test_garbled_lines_are_skipped_but_listed(self):
        text = ("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n"
                "abc, def, ghi\n\nRTX 3050, 4096, 300\n")
        gpus, problems = bench.parse_nvidia_smi(text)
        self.assertEqual([g["name"] for g in gpus], ["RTX 3050"])
        self.assertEqual(len(problems), 2)

    def test_empty_output(self):
        self.assertEqual(bench.parse_nvidia_smi(""), ([], []))
        self.assertEqual(bench.parse_nvidia_smi(None), ([], []))


class QueryGpuTests(unittest.TestCase):
    def test_missing_nvidia_smi_does_not_raise(self):
        with patch.object(bench, "find_nvidia_smi", return_value=None):
            info = bench.query_gpus(runner=lambda *a, **k: self.fail("không được chạy gì"))
        self.assertFalse(info["available"])
        self.assertIn("Không tìm thấy nvidia-smi", info["note"])

    def test_runner_errors_become_notes(self):
        def not_found(*a, **k):
            raise FileNotFoundError("nvidia-smi")

        def hangs(*a, **k):
            raise subprocess.TimeoutExpired("nvidia-smi", 5)

        for runner, needle in [(not_found, "không tồn tại"), (hangs, "không trả lời")]:
            info = bench.query_gpus(runner=runner, smi_path="nvidia-smi")
            self.assertFalse(info["available"])
            self.assertIn(needle, info["note"])

    def test_driver_failure_shows_first_stderr_line(self):
        def broken(*a, **k):
            return SimpleNamespace(returncode=9, stdout="", stderr=(
                "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.\n"
                "Make sure that the latest NVIDIA driver is installed and running.\n"))
        info = bench.query_gpus(runner=broken, smi_path="nvidia-smi")
        self.assertFalse(info["available"])
        self.assertIn("mã 9", info["note"])
        self.assertIn("couldn't communicate", info["note"])

    def test_garbled_success_output_is_not_available(self):
        info = bench.query_gpus(
            runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="???\n", stderr=""),
            smi_path="nvidia-smi")
        self.assertFalse(info["available"])
        self.assertEqual(len(info["problems"]), 1)

    def test_success_passes_exact_query(self):
        seen = {}

        def ok(args, **kwargs):
            seen["args"], seen["timeout"] = args, kwargs.get("timeout")
            return SimpleNamespace(returncode=0, stdout=SMI_LINE.format(used=312), stderr="")
        info = bench.query_gpus(runner=ok, smi_path="nvidia-smi")
        self.assertTrue(info["available"])
        self.assertEqual(seen["args"], ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                                        "--format=csv,noheader,nounits"])
        self.assertEqual(seen["timeout"], bench.NVIDIA_SMI_TIMEOUT_S)

    def test_windows_fallback_locations(self):
        self.assertEqual(bench.find_nvidia_smi(which=lambda n: None, candidates=("a", "b"),
                                               exists=lambda p: p == "b"), "b")
        self.assertIsNone(bench.find_nvidia_smi(which=lambda n: None, candidates=("a",),
                                                exists=lambda p: False))


# ══════════════════════════════════════════════════════════════════════════
# 2. RAM + đĩa
# ══════════════════════════════════════════════════════════════════════════
class RamTests(unittest.TestCase):
    def test_parse_meminfo(self):
        info = bench.parse_meminfo("MemTotal:       32768000 kB\nMemFree: 1 kB\n"
                                   "MemAvailable:   16384000 kB\n")
        self.assertEqual(info, {"total_mib": 32000.0, "available_mib": 16000.0})
        self.assertEqual(bench.parse_meminfo("rác"), {"total_mib": None, "available_mib": None})

    def test_unsupported_platform_is_a_note_not_a_crash(self):
        info = bench.read_ram(platform_name="sunos5")
        self.assertIsNone(info["total_mib"])
        self.assertIn("psutil", info["note"])

    @unittest.skipUnless(sys.platform.startswith("linux") or sys.platform == "win32",
                         "chỉ đọc được RAM thật trên Linux/Windows")
    def test_real_ram_is_readable_here(self):
        info = bench.read_ram()
        self.assertGreater(info["total_mib"], 0)
        self.assertLessEqual(info["available_mib"], info["total_mib"])


class DiskTests(unittest.TestCase):
    def test_threshold_is_inclusive(self):
        self.assertTrue(bench.disk_verdict(30 * GIB)["enough"])
        self.assertFalse(bench.disk_verdict(30 * GIB - 1)["enough"])
        self.assertTrue(bench.disk_verdict(120 * GIB)["enough"])
        self.assertEqual(bench.disk_verdict(31 * GIB)["required_gib"], 30.0)
        self.assertFalse(bench.disk_verdict(31 * GIB, required_gib=40)["enough"])

    def test_threshold_covers_next_phase_models_plus_margin(self):
        self.assertGreater(bench.REQUIRED_FREE_GIB, sum(bench.NEXT_PHASE_MODELS_GIB.values()))

    def test_missing_models_dir_measures_nearest_parent(self):
        Usage = namedtuple("Usage", "total used free")
        with tempfile.TemporaryDirectory() as tmp:
            seen = []
            info = bench.check_disk(os.path.join(tmp, "ComfyUI", "models"),
                                    usage=lambda p: seen.append(p) or Usage(100 * GIB, 75 * GIB, 25 * GIB))
            self.assertEqual(seen, [os.path.realpath(tmp)])
        self.assertFalse(info["models_dir_exists"])
        self.assertFalse(info["enough"])
        self.assertEqual(info["free_gib"], 25.0)

    def test_usage_error_is_reported(self):
        def broken(path):
            raise PermissionError("Access is denied")
        with tempfile.TemporaryDirectory() as tmp:
            info = bench.check_disk(tmp, usage=broken)
        self.assertIsNone(info["enough"])
        self.assertIn("Access is denied", info["error"])


# ══════════════════════════════════════════════════════════════════════════
# 3. Lấy mẫu VRAM + thống kê
# ══════════════════════════════════════════════════════════════════════════
class SamplerTests(unittest.TestCase):
    def test_peak_errors_and_mark(self):
        values = iter([100, 300, None, 900, RuntimeError("nvidia-smi treo")])

        def probe():
            try:
                value = next(values)
            except StopIteration:
                return 400
            if isinstance(value, Exception):
                raise value
            return value

        sampler = bench.ResourceSampler(vram_probe=probe, ram_probe=lambda: 2048.0, interval=0.005)
        sampler.start()
        deadline = time.time() + 2
        while len(sampler.samples) < 6 and time.time() < deadline:
            time.sleep(0.005)
        sampler.mark("unload_done")
        time.sleep(0.03)
        summary = sampler.stop()
        self.assertFalse(sampler._thread.is_alive())
        self.assertEqual(summary["vram_peak_mib"], 900)
        self.assertEqual(summary["vram_first_mib"], 100)
        self.assertEqual(summary["vram_peak_after_unload_mib"], 400)   # đỉnh sau mark
        self.assertEqual(summary["probe_errors"], 1)                   # lỗi được đếm, không nuốt
        self.assertEqual(summary["first_error"], "nvidia-smi treo")
        self.assertEqual(summary["ram_min_available_mib"], 2048.0)
        self.assertEqual(summary["vram_series"][0][1], 100)

    def test_without_probes_still_counts_samples(self):
        sampler = bench.ResourceSampler(interval=0.005).start()
        time.sleep(0.02)
        summary = sampler.stop()
        self.assertGreater(summary["samples"], 0)
        self.assertIsNone(summary["vram_peak_mib"])
        self.assertIsNone(summary["ram_min_available_mib"])


def _job(index, wall, exec_s=None, ok=True, checkpoint_cached=None, sampler_cached=False,
         peak=None, unload_s=0.01, unloaded=(), before=None):
    return {"index": index, "role": "cold" if index == 1 else "warm", "seed": 100 + index,
            "ok": ok, "wall_s": wall, "exec_s": exec_s, "checkpoint_cached": checkpoint_cached,
            "sampler_cached": sampler_cached, "unload_s": unload_s, "unloaded": list(unloaded),
            "ollama_before": before,
            "resources": {"vram_peak_mib": peak, "vram_peak_after_unload_mib": peak,
                          "ram_min_available_mib": 20000.0}}


class StatsTests(unittest.TestCase):
    def test_time_stats(self):
        self.assertIsNone(bench.time_stats([]))
        self.assertIsNone(bench.time_stats([None]))
        self.assertEqual(bench.time_stats([10, None, 30, 20]),
                         {"n": 3, "mean": 20.0, "median": 20.0, "min": 10.0, "max": 30.0})

    def test_cold_and_warm_are_split(self):
        jobs = [_job(1, 48.3, 45.1, checkpoint_cached=False, peak=3650),
                _job(2, 18.5, 16.2, checkpoint_cached=True, peak=3400),
                _job(3, 17.9, 15.8, checkpoint_cached=True, peak=3380)]
        summary = bench.summarize_jobs(jobs, True)
        self.assertEqual(summary["cold"]["wall_s"], 48.3)
        self.assertTrue(summary["cold"]["verified_cold"])
        self.assertEqual(summary["cold"]["vram_peak_mib"], 3650)
        self.assertEqual(summary["warm"]["wall_s"]["n"], 2)
        self.assertEqual(summary["warm"]["wall_s"]["mean"], 18.2)
        self.assertEqual(summary["warm"]["exec_s"]["mean"], 16.0)
        self.assertEqual(summary["warm"]["vram_peak_mib"], 3400)

    def test_single_run_has_no_warm(self):
        summary = bench.summarize_jobs([_job(1, 40.0)], True)
        self.assertIsNone(summary["warm"])
        self.assertIsNone(summary["cold"]["verified_cold"])   # không có /history → chưa xác nhận

    def test_cached_sampler_job_is_excluded(self):
        jobs = [_job(1, 40.0, checkpoint_cached=False), _job(2, 0.5, sampler_cached=True),
                _job(3, 18.0)]
        summary = bench.summarize_jobs(jobs, True)
        self.assertEqual(summary["jobs_invalid_cached"], [2])
        self.assertEqual(summary["warm"]["wall_s"]["n"], 1)

    def test_d16_summary(self):
        before = [{"name": "qwen2.5:3b", "size_mib": 2355.2, "vram_mib": 2048.0}]
        jobs = [_job(1, 40.0, unload_s=0.4, unloaded=["qwen2.5:3b"], before=before),
                _job(2, 18.0, unload_s=0.02, before=[])]
        d16 = bench.summarize_d16(jobs, True)
        self.assertTrue(d16["ollama_reachable"])
        self.assertTrue(d16["had_models_in_vram"])
        self.assertEqual(d16["unloaded_models"], ["qwen2.5:3b"])
        self.assertEqual(d16["vram_before_max_mib"], 2048.0)
        self.assertEqual(d16["unload_s"]["max"], 0.4)
        down = bench.summarize_d16([_job(1, 40.0, before=None)], True)
        self.assertFalse(down["ollama_reachable"])
        self.assertFalse(down["had_models_in_vram"])

    def test_history_parsing(self):
        parsed = bench.parse_history_entry(_history("p", 1_000, 46_100, ["1"]).json()["p"])
        self.assertEqual(parsed, {"comfy_status": "success", "exec_s": 45.1, "cached_nodes": ["1"]})
        self.assertEqual(bench.parse_history_entry(None),
                         {"comfy_status": None, "exec_s": None, "cached_nodes": None})
        garbled = {"status": {"status_str": "error", "messages": ["rác", [1], ["execution_start", 5]]}}
        self.assertEqual(bench.parse_history_entry(garbled)["exec_s"], None)
        self.assertEqual(bench.parse_history_entry(garbled)["comfy_status"], "error")


# ══════════════════════════════════════════════════════════════════════════
# 4. Workload: đúng đường REST, không persona, không ghi đè
# ══════════════════════════════════════════════════════════════════════════
class WorkloadTests(unittest.TestCase):
    def test_workflow_is_the_rest_workflow_with_separate_output(self):
        prompt, workflows = bench.build_workloads("fake_sd15.safetensors", [7, 8])
        for seed, wf in zip([7, 8], workflows):
            expected = comfy_service.build_quality_workflow(
                "fake_sd15.safetensors", prompt, bench.NEGATIVE_PROMPT, seed,
                quality=comfy_service.QUALITY_BALANCED, upscale=comfy_service.UPSCALE_NONE)
            saves = [n for n in expected.values() if n["class_type"] == "SaveImage"]
            self.assertEqual(len(saves), 1)
            saves[0]["inputs"]["filename_prefix"] = bench.BENCH_FILENAME_PREFIX
            self.assertEqual(wf, expected)   # khác REST đúng một chỗ: nơi ComfyUI lưu PNG

    def test_bench_uses_the_rest_defaults(self):
        """Đọc main.py bằng ast (không import FastAPI): REST đổi mặc định mà bench không đổi
        theo thì số đo không còn là "đường REST" nữa."""
        tree = ast.parse(Path(__file__).with_name("main.py").read_text(encoding="utf-8"))
        img_req = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ImgReq")
        defaults = {f.target.id: f.value for f in img_req.body if isinstance(f, ast.AnnAssign)}
        self.assertEqual(ast.literal_eval(defaults["style"]), bench.BENCH_STYLE)
        self.assertEqual(defaults["quality"].id, "QUALITY_BALANCED")
        self.assertEqual(defaults["upscale"].id, "UPSCALE_NONE")
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "build_quality_workflow"]
        self.assertEqual(len(calls), 1)
        # REST không truyền width/height → 512x768 mặc định, đúng cỡ bench đo.
        self.assertEqual(sorted(k.arg for k in calls[0].keywords), ["quality", "upscale"])
        self.assertEqual(len(calls[0].args), 4)

    def test_prompt_is_persona_free_and_fixed(self):
        prompt, workflows = bench.build_workloads("m.safetensors", [1])
        # Đúng ba phần nội dung → style → chất lượng; KHÔNG có phần ngoại hình đứng đầu.
        self.assertEqual(prompt, ", ".join([bench.BENCH_SUBJECT, image_prompt.style_tags("Anime"),
                                            image_prompt.QUALITY_SUFFIX]))
        self.assertEqual(workflows[0]["2"]["inputs"]["text"], prompt)
        self.assertEqual(workflows[0]["3"]["inputs"]["text"], image_prompt.NEGATIVE_PROMPT)
        self.assertIn("no humans", prompt)
        for persona in PERSONAS.values():
            self.assertNotIn(persona.get("appearance", "") or "\0", prompt)
            self.assertNotIn(persona.get("name", "") or "\0", prompt)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bench.parse_args(["--prompt", "bất kỳ"])   # cố ý không có tham số này

    def test_workload_description_reads_the_workflow(self):
        prompt, workflows = bench.build_workloads("m.safetensors", [5])
        wl = bench.describe_workload("m.safetensors", prompt, workflows[0], [5])
        preset = comfy_service.QUALITY_PRESETS[comfy_service.QUALITY_BALANCED]
        self.assertEqual((wl["steps"], wl["cfg"], wl["sampler"]),
                         (preset["steps"], preset["cfg"], preset["sampler"]))
        self.assertEqual((wl["width"], wl["height"]), (512, 768))
        self.assertEqual((wl["checkpoint_node"], wl["sampler_node"]), ("1", "5"))

    def test_workflow_without_save_node_stops_the_bench(self):
        with self.assertRaises(RuntimeError):
            bench.retarget_save_nodes({"1": {"class_type": "KSampler", "inputs": {}}})

    def test_runtime_seams_still_exist(self):
        """Bench bọc hai tên này trong runtime; đổi tên mà không sửa bench là test này đỏ."""
        self.assertTrue(callable(runtime.unload_ollama_models))
        self.assertTrue(callable(runtime._raw_submit_workflow))
        with patch.object(runtime, "_raw_submit_workflow", None), self.assertRaises(RuntimeError):
            with bench.instrument_runtime({}):
                pass

    def test_instrumentation_is_always_removed(self):
        orig = (runtime.unload_ollama_models, runtime._raw_submit_workflow)
        with self.assertRaises(ValueError):
            with bench.instrument_runtime({}):
                raise ValueError("job lỗi")
        self.assertEqual((runtime.unload_ollama_models, runtime._raw_submit_workflow), orig)

    def test_args_validation(self):
        args = bench.parse_args([])
        self.assertEqual((args.runs, args.seed), (3, bench.DEFAULT_SEED))
        self.assertFalse(args.warm_ollama or args.no_free or args.check_only)
        for bad in (["--runs", "0"], ["--runs", "21"], ["--runs", "ba"], ["--seed", "-1"],
                    ["--timeout", "5"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bench.parse_args(bad)

    def test_output_files_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bench"
            first = bench.plan_output_paths(out, "20261021-200000")
            bench.write_results({"a": 1}, *first, "khối\n")
            second = bench.plan_output_paths(out, "20261021-200000")
            self.assertNotEqual(first, second)
            self.assertTrue(second[0].name.endswith("-2.json"))
            with self.assertRaises(FileExistsError):
                bench.write_results({"a": 2}, *first, "x")
            self.assertEqual(json.loads(first[0].read_text(encoding="utf-8")), {"a": 1})


# ══════════════════════════════════════════════════════════════════════════
# 5. Chạy trọn main() với ComfyUI/Ollama/nvidia-smi giả
# ══════════════════════════════════════════════════════════════════════════
class MainFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "bench"
        self.events = []
        self.submitted = []
        self.smi = _FakeSmi()
        self.fail_on_job = None
        self.styles = []

    def tearDown(self):
        _FakeClient.events = None
        self.tmp.cleanup()

    def _fake_submit(self, workflow):
        self.submitted.append(workflow)
        self.events.append(("submit", len(self.submitted)))
        return f"pid-{len(self.submitted)}"

    def _fake_poll(self, prompt_id, progress=None, start_pct=0.3, timeout=300):
        self.events.append(("poll", prompt_id, timeout))
        if self.fail_on_job == len(self.submitted):
            raise comfy_service.ComfyUIError("Timeout — ComfyUI chạy quá lâu")
        self.smi.used = 3500            # ComfyUI đang vẽ
        time.sleep(0.06)
        self.smi.used = 900
        return _FakeImage()

    def _run(self, argv, *, comfy=None, ollama=None):
        routes = {}
        routes.update(comfy if comfy is not None else comfy_routes())
        routes.update(ollama if ollama is not None else ollama_routes())
        _FakeClient.calls, _FakeClient.routes, _FakeClient.events = [], routes, self.events
        out = io.StringIO()
        env_before = dict(os.environ)
        storage_loaded_before = "yumeai.storage" in sys.modules
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(httpx, "Client", _FakeClient))
            # Chỉ thay `subprocess` mà bench thấy: thay subprocess.run toàn cục thì cả
            # platform.platform() (gọi `uname -p`) cũng nhận output nvidia-smi giả.
            stack.enter_context(patch.object(bench, "subprocess", SimpleNamespace(
                run=self.smi, TimeoutExpired=subprocess.TimeoutExpired)))
            stack.enter_context(patch.object(bench, "find_nvidia_smi", return_value="nvidia-smi"))
            stack.enter_context(patch.object(bench, "SAMPLE_INTERVAL_S", 0.01))
            stack.enter_context(patch.object(bench, "FREE_SETTLE_S", 0))
            stack.enter_context(patch.object(bench, "VRAM_HANDOFF", True))
            stack.enter_context(patch.object(ollama_service, "VRAM_HANDOFF", True))
            stack.enter_context(patch.object(
                comfy_service, "get_checkpoint_model",
                side_effect=lambda style: self.styles.append(style) or "fake_sd15.safetensors"))
            stack.enter_context(patch.object(runtime, "_raw_submit_workflow", self._fake_submit))
            stack.enter_context(patch.object(runtime, "_raw_poll_and_download", self._fake_poll))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = bench.main([*argv, "--out-dir", str(self.out_dir)])
            seams_after = (runtime.unload_ollama_models, runtime._raw_submit_workflow)
            self.assertEqual(seams_after, (ollama_service.unload_ollama_models, self._fake_submit),
                             "bench phải trả lại đúng hàm của runtime sau khi đo")
        self.assertEqual(dict(os.environ), env_before, "bench không được đổi biến môi trường")
        if not storage_loaded_before:
            self.assertNotIn("yumeai.storage", sys.modules, "bench không được đụng tới DB")
        self.assertIsNone(runtime._YUMEAI_JOB_STATE["owner"], "Resource Guard phải được nhả")
        return code, out.getvalue()

    def _result(self):
        files = sorted(self.out_dir.iterdir())
        self.assertEqual(len(files), 2, files)
        data = json.loads(next(f for f in files if f.suffix == ".json").read_text(encoding="utf-8"))
        md = next(f for f in files if f.name.endswith("_testlog.md")).read_text(encoding="utf-8")
        return data, md

    def test_full_run_goes_through_run_comfy_job(self):
        code, out = self._run(["--runs", "3", "--seed", "100", "--timeout", "600"])
        self.assertEqual(code, 0, out)
        data, md = self._result()

        self.assertEqual(self.styles, ["Anime"])   # cùng style mặc định với ImgReq
        self.assertEqual([j["seed"] for j in data["jobs"]], [100, 101, 102])
        self.assertEqual([j["role"] for j in data["jobs"]], ["cold", "warm", "warm"])
        self.assertTrue(all(j["ok"] for j in data["jobs"]))
        for wf in self.submitted:
            prefixes = [n["inputs"]["filename_prefix"] for n in wf.values()
                        if n["class_type"] == "SaveImage"]
            self.assertEqual(prefixes, ["yumeai_bench/pc0"])
        # /free đúng MỘT lần, trước job đầu tiên; timeout của CLI đi tới tận poll.
        frees = [i for i, e in enumerate(self.events) if e[0] == "POST" and e[1].endswith("/free")]
        first_submit = self.events.index(("submit", 1))
        self.assertEqual(len(frees), 1)
        self.assertLess(frees[0], first_submit)
        self.assertEqual([e[2] for e in self.events if e[0] == "poll"], [600] * 3)
        # D16 chạy BÊN TRONG run_comfy_job (unload trước submit) và được đo.
        unload_posts = [i for i, e in enumerate(self.events)
                        if e[0] == "POST" and e[1].endswith("/api/generate")]
        self.assertLess(unload_posts[0], first_submit)
        job1 = data["jobs"][0]
        self.assertEqual(job1["unloaded"], ["qwen2.5:3b"])
        self.assertIsInstance(job1["unload_s"], float)
        self.assertEqual(job1["ollama_before"][0]["vram_mib"], 2048.0)
        self.assertEqual(job1["prompt_id"], "pid-1")
        # cold/warm theo /history, VRAM đỉnh theo nvidia-smi giả.
        summary = data["summary"]
        self.assertEqual(summary["cold"]["exec_s"], 45.1)
        self.assertTrue(summary["cold"]["verified_cold"])
        self.assertEqual(summary["cold"]["vram_peak_mib"], 3500)
        self.assertEqual(summary["warm"]["exec_s"]["mean"], 16.0)
        self.assertTrue(summary["d16"]["had_models_in_vram"])
        self.assertEqual(data["free_before_first_job"]["vram_used_after_mib"], 300)
        self.assertEqual(data["environment"]["gpu"]["gpus"][0]["total_mib"], 4096)
        self.assertTrue(data["services"]["comfyui"]["lowvram"])
        self.assertEqual(data["schema"], bench.BENCH_SCHEMA)
        # Khối TESTLOG: in ra màn hình và ghi file là MỘT nội dung.
        self.assertIn(md, out)
        self.assertIn("text2img SD1.5 — job 1 (cold, nạp checkpoint) | pass | ", md)
        self.assertIn("| text2img SD1.5 — warm (2 job) | pass |", md)
        self.assertIn("đã nhả qwen2.5:3b", md)

    def test_testlog_block_follows_template(self):
        self._run(["--runs", "2"])
        _, md = self._result()
        lines = md.splitlines()
        self.assertRegex(lines[0], r"^## \d{4}-\d{2}-\d{2} — NVIDIA GeForce RTX 3050 Laptop GPU 4096 MiB, ")
        headings = [ln for ln in lines if ln.startswith("**")]
        self.assertEqual(headings, ["**Phần cứng**", "**Cấu hình chạy**", "**Kết quả**",
                                    "**Lỗi gặp phải**", "**Kết luận / cần đổi gì trong code**"])
        self.assertIn("| Hạng mục | Kết quả | Thời gian | Ghi chú |\n|---|---|---|---|", md)
        for label in ("- GPU: ", "- VRAM: ", "- RAM: ", "- HĐH: ", "- OLLAMA_MODEL: ",
                      "- Model ComfyUI (checkpoint / Kontext quant): ", "- Biến môi trường đã đổi: "):
            self.assertTrue(any(ln.startswith(label) for ln in lines), label)
        rows = [ln for ln in lines if ln.startswith("| ") and not ln.startswith("| Hạng mục")]
        for row in rows:
            cells = re.split(r"(?<!\\)\|", row)[1:-1]
            self.assertEqual(len(cells), 4, row)
        # Chỉ ghi thứ bench THẬT SỰ đo — không có dòng trống cho Flux/inpaint/SDXL.
        for absent in ("Flux", "inpaint", "SDXL", "~?s"):
            self.assertNotIn(absent, md)
        self.assertIn("- Không có.", md)

    def test_comfyui_down_stops_before_anything(self):
        comfy = {"/system_stats": DOWN, "/queue": DOWN, "/free": DOWN}
        code, out = self._run(["--runs", "2"], comfy=comfy)
        self.assertEqual(code, 2)
        self.assertIn("Không kết nối được ComfyUI", out)
        self.assertIn("python main.py --lowvram", out)
        self.assertEqual(self.submitted, [])
        self.assertFalse(self.out_dir.exists(), "không được ghi file khi chưa đo gì")

    def test_ollama_down_is_not_fatal(self):
        ollama = {"/api/tags": DOWN, "/api/ps": DOWN, "/api/generate": DOWN}
        code, out = self._run(["--runs", "1"], ollama=ollama)
        self.assertEqual(code, 0, out)
        data, md = self._result()
        self.assertFalse(data["services"]["ollama"]["up"])
        self.assertFalse(data["summary"]["d16"]["ollama_reachable"])
        self.assertIsNone(data["jobs"][0]["ollama_before"])
        self.assertIn("| D16 — nhả VRAM Ollama trước job | Ollama không chạy |", md)
        self.assertIn("| text2img SD1.5 — warm | chưa đo | — | chạy với --runs 1 |", md)

    def test_ollama_with_nothing_in_vram(self):
        code, _ = self._run(["--runs", "1"], ollama=ollama_routes(in_vram=False))
        self.assertEqual(code, 0)
        data, md = self._result()
        self.assertEqual(data["jobs"][0]["unloaded"], [])
        self.assertIn("không có gì để nhả", md)
        self.assertIn("--warm-ollama", md)   # gợi ý cách đo D16 thật

    def test_check_only_runs_no_job_and_writes_nothing(self):
        code, out = self._run(["--check-only"])
        self.assertEqual(code, 0)
        self.assertIn("RTX 3050", out)
        self.assertEqual(self.submitted, [])
        self.assertFalse(self.out_dir.exists())

    def test_busy_comfyui_queue_is_refused(self):
        code, out = self._run(["--runs", "1"], comfy=comfy_routes(queue_running=1))
        self.assertEqual(code, 2)
        self.assertIn("đang có 1 job chạy", out)
        self.assertEqual(self.submitted, [])

    @unittest.skipUnless(runtime.YUMEAI_RESOURCE_GUARD, "YUMEAI_RESOURCE_GUARD=0")
    def test_resource_guard_held_elsewhere_is_refused(self):
        held, release = threading.Event(), threading.Event()

        def other_job():
            with contextlib.redirect_stdout(io.StringIO()):
                runtime.acquire_generation_slot("job khác")
                held.set()
                release.wait(5)
                runtime.release_generation_slot()

        worker = threading.Thread(target=other_job)
        worker.start()
        held.wait(5)
        try:
            _FakeClient.calls, _FakeClient.routes = [], {**comfy_routes(), **ollama_routes()}
            out = io.StringIO()
            with patch.object(httpx, "Client", _FakeClient), \
                    patch.object(bench, "find_nvidia_smi", return_value=None), \
                    patch.object(comfy_service, "get_checkpoint_model", return_value="m.safetensors"), \
                    patch.object(runtime, "_raw_submit_workflow", self._fake_submit), \
                    contextlib.redirect_stdout(out):
                code = bench.main(["--runs", "1", "--out-dir", str(self.out_dir)])
        finally:
            release.set()
            worker.join(5)
        self.assertEqual(code, 2)
        self.assertIn("đang xử lý job khác", out.getvalue())
        self.assertEqual(self.submitted, [])

    def test_failed_job_keeps_partial_results(self):
        self.fail_on_job = 2
        code, out = self._run(["--runs", "3"])
        self.assertEqual(code, 1)
        data, md = self._result()
        self.assertEqual([j["ok"] for j in data["jobs"]], [True, False])   # dừng sau job lỗi
        self.assertIn("Timeout", data["jobs"][1]["error"])
        self.assertIn("- Job 2 (seed", md)
        self.assertIn("| text2img SD1.5 — warm | fail |", md)
        self.assertIn("❌", out)

    def test_cached_sampler_is_flagged(self):
        code, out = self._run(["--runs", "2"], comfy=comfy_routes(sampler_cached_job=2))
        self.assertEqual(code, 0)
        data, md = self._result()
        self.assertTrue(data["jobs"][1]["sampler_cached"])
        self.assertEqual(data["summary"]["jobs_invalid_cached"], [2])
        self.assertIn("KSampler lấy từ cache", out)
        self.assertIn("- Job 2: KSampler lấy từ cache ComfyUI → không tính.", md)

    def test_no_free_and_warm_ollama_flags(self):
        warmups = []
        with patch.object(ollama_service, "warmup_ollama", lambda: warmups.append(1)):
            code, _ = self._run(["--runs", "2", "--no-free", "--warm-ollama"])
        self.assertEqual(code, 0)
        self.assertEqual(len(warmups), 2)   # trước MỖI job
        self.assertFalse(any(e[0] == "POST" and e[1].endswith("/free") for e in self.events))
        data, md = self._result()
        self.assertFalse(data["free_before_first_job"]["requested"])
        self.assertIn("/free trước job 1: không; --warm-ollama: có", md)

    def test_comfyui_without_lowvram_is_called_out(self):
        code, out = self._run(["--runs", "1"], comfy=comfy_routes(argv=("main.py",)))
        self.assertEqual(code, 0)
        _, md = self._result()
        self.assertIn("KHÔNG có --lowvram", out)
        self.assertIn("KHÔNG chạy `--lowvram`", md)


if __name__ == "__main__":
    unittest.main()
