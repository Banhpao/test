"""Test offline cho số đo hiệu năng thật (lộ trình P3.5): yumeai/core/perf.py, phần ghi số
trong /api/chat và /api/images/generate, và GET /api/stats. Không cần Ollama hay ComfyUI."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import main
from yumeai.core.perf import PerfLog, ollama_chunk_stats
from yumeai.core.personas import PERSONAS
from yumeai.core.runtime import ResourceBusyError
from yumeai.services.comfy_service import ComfyUIError
from yumeai.storage import YumeDatabase

SECRET_MESSAGE = "bí mật nhỏ của anh"
SECRET_PROMPT = "Pao ngồi bên cửa sổ đọc thư tình"

# Chunk cuối thật của Ollama /api/chat mang số đo bằng nano giây.
FINAL_CHUNK = {
    "message": {"content": ""}, "done": True, "done_reason": "stop",
    "eval_count": 40, "eval_duration": 2_000_000_000,
    "prompt_eval_count": 600, "prompt_eval_duration": 500_000_000,
    "load_duration": 3_000_000_000, "total_duration": 6_000_000_000,
}


class _FakeChatResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield json.dumps({"message": {"content": "Hừ, "}, "done": False})
        yield json.dumps({"message": {"content": "chào."}, "done": False})
        yield json.dumps(FINAL_CHUNK)


class _CutOffChatResponse(_FakeChatResponse):
    """Ollama đóng kết nối giữa chừng, không gửi chunk done."""

    async def aiter_lines(self):
        yield json.dumps({"message": {"content": "Hừ"}, "done": False})


class _BrokenChatResponse(_FakeChatResponse):
    async def __aenter__(self):
        raise RuntimeError("Ollama không phản hồi")


def _client_returning(response_cls):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, *args, **kwargs):
            return response_cls()

    return _Client


class OllamaChunkStatsTests(unittest.TestCase):
    def test_converts_nanoseconds_to_rates(self):
        stats = ollama_chunk_stats(FINAL_CHUNK)
        self.assertEqual(stats["output_tokens"], 40)
        self.assertEqual(stats["tokens_per_s"], 20.0)
        self.assertEqual(stats["prompt_tokens"], 600)
        self.assertEqual(stats["prompt_tokens_per_s"], 1200.0)
        self.assertEqual(stats["load_s"], 3.0)
        self.assertEqual(stats["ollama_total_s"], 6.0)
        self.assertEqual(stats["done_reason"], "stop")

    def test_missing_or_odd_fields_are_skipped(self):
        self.assertEqual(ollama_chunk_stats({"done": True}), {})
        odd = ollama_chunk_stats({"eval_count": True, "eval_duration": "1", "prompt_eval_count": 0,
                                  "prompt_eval_duration": 0, "done_reason": ""})
        # 0 token prompt (cache trúng hết) vẫn ghi số token nhưng không chia cho 0.
        self.assertEqual(odd, {"prompt_tokens": 0})


class PerfLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "perf.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self, path=None):
        return [json.loads(x) for x in (path or self.path).read_text(encoding="utf-8").splitlines()]

    def test_record_appends_one_json_line_without_empty_fields(self):
        log = PerfLog(self.path)
        log.record("chat", status="ok", model="qwen2.5:3b", tokens_per_s=18.5, error=None)
        log.record("image", status="ok", quality="Cao (35 steps)")
        rows = self.lines()
        self.assertEqual([r["kind"] for r in rows], ["chat", "image"])
        self.assertEqual(rows[0]["v"], 1)
        self.assertIn("ts", rows[0])
        self.assertNotIn("error", rows[0])

    def test_disabled_log_writes_nothing(self):
        log = PerfLog(self.path, enabled=False)
        self.assertIsNone(log.record("chat", status="ok"))
        self.assertFalse(self.path.exists())
        self.assertFalse(log.summarize()["enabled"])

    def test_rotates_and_reads_back_across_both_files(self):
        log = PerfLog(self.path, max_bytes=400)
        for i in range(12):
            log.record("chat", status="ok", model="m", turn=i)
        self.assertTrue(log.rotated_path.exists())
        self.assertLessEqual(self.path.stat().st_size, 400)
        turns = [r["turn"] for r in log.records(limit=1000)]
        self.assertEqual(turns, sorted(turns))
        self.assertEqual(turns[-1], 11)
        self.assertEqual([r["turn"] for r in log.records(limit=3)], [9, 10, 11])

    def test_broken_lines_are_ignored(self):
        log = PerfLog(self.path)
        log.record("chat", status="ok", model="m")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write('{"kind": "chat", "status": "o')   # mất điện giữa lúc ghi
            fh.write("\n[1, 2]\n")
        log.record("chat", status="ok", model="m")
        self.assertEqual(len(log.records()), 2)

    def test_write_failure_never_raises_and_warns_once(self):
        self.path.mkdir(parents=True)   # đường dẫn là thư mục → mở để ghi sẽ lỗi
        log = PerfLog(self.path)
        out = StringIO()
        with redirect_stdout(out):
            self.assertIsNone(log.record("chat", status="ok"))
            self.assertIsNone(log.record("chat", status="ok"))
        self.assertEqual(out.getvalue().count("Không ghi được số đo"), 1)
        self.assertEqual(log.records(), [])

    def test_summary_groups_by_model_and_by_preset(self):
        log = PerfLog(self.path)
        for tps, first in ((10.0, 2.0), (20.0, 1.0), (30.0, 3.0)):
            log.record("chat", status="ok", model="qwen2.5:3b", tokens_per_s=tps, first_token_s=first,
                       total_s=first + 4, prompt_tokens=500, load_s=0.01)
        log.record("chat", status="ok", model="qwen2.5:3b", tokens_per_s=5.0, first_token_s=9.0,
                   total_s=12.0, done_reason="length", load_s=4.2)
        log.record("chat", status="error", model="qwen2.5:3b", error="ConnectError", total_s=0.1)
        log.record("chat", status="cancelled", model="qwen2.5:3b", total_s=1.0)
        log.record("chat", status="ok", model="hermes3:3b", tokens_per_s=15.0, first_token_s=1.5, total_s=6.0)
        best = "Tốt nhất (50 steps)"
        for total in (200.0, 180.0, 240.0):
            log.record("image", status="ok", quality=best, upscale="Không upscale", total_s=total,
                       comfy_s=total - 10, translate_s=10.0, model="anything-v5.safetensors")
        log.record("image", status="error", quality=best, upscale="Không upscale", total_s=5.0, error="comfy")
        log.record("image", status="busy", quality=best, upscale="2x (ESRGAN)", total_s=0.2)

        summary = log.summarize()

        self.assertEqual(summary["measurements"], 12)
        models = summary["chat"]["models"]
        self.assertEqual([m["model"] for m in models], ["hermes3:3b", "qwen2.5:3b"])   # mới dùng lên đầu
        qwen = models[1]
        self.assertEqual((qwen["turns"], qwen["ok"], qwen["failed"], qwen["cancelled"]), (6, 4, 1, 1))
        self.assertEqual((qwen["truncated"], qwen["reloads"]), (1, 1))
        self.assertEqual(qwen["tokens_per_s"], {"n": 4, "median": 15.0, "min": 5.0, "max": 30.0})
        self.assertEqual(qwen["first_token_s"]["median"], 2.5)

        presets = {(p["quality"], p["upscale"]): p for p in summary["image"]["presets"]}
        plain = presets[(best, "Không upscale")]
        self.assertEqual((plain["count"], plain["ok"], plain["failed"]), (4, 3, 1))
        # Lần lỗi (5 giây) không được kéo trung vị xuống.
        self.assertEqual(plain["total_s"], {"n": 3, "median": 200.0, "min": 180.0, "max": 240.0})
        self.assertEqual(plain["models"], ["anything-v5.safetensors"])
        upscaled = presets[(best, "2x (ESRGAN)")]
        self.assertEqual((upscaled["busy"], upscaled["total_s"]), (1, None))

    def test_empty_summary(self):
        summary = PerfLog(self.path).summarize()
        self.assertEqual((summary["measurements"], summary["since"]), (0, None))
        self.assertEqual(summary["chat"], {"turns": 0, "models": []})
        self.assertEqual(summary["image"], {"count": 0, "presets": []})


class _ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = YumeDatabase(Path(self.tmp.name) / "api.db")
        self.database.initialize(PERSONAS)
        self.images_dir = Path(self.tmp.name) / "images"
        self.images_dir.mkdir()
        self.perf = PerfLog(Path(self.tmp.name) / "perf.jsonl")
        self.old = (main.database, main.perf_log)
        main.database, main.perf_log = self.database, self.perf

    def tearDown(self):
        main.database, main.perf_log = self.old
        self.tmp.cleanup()

    def client(self):
        return TestClient(main.app)

    def lifespan(self):
        return (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
        )

    def raw_log(self):
        return self.perf.path.read_text(encoding="utf-8")


class ChatPerfTests(_ApiCase):
    def chat(self, response_cls, conversation=True):
        a, b = self.lifespan()
        conv = self.database.create_conversation("user_001", "pao", display_name="Minh") if conversation else None
        with a, b, patch.object(main.httpx, "AsyncClient", _client_returning(response_cls)), self.client() as client:
            return client.post("/api/chat", json={
                "message": SECRET_MESSAGE, "persona_id": "pao", "user_id": "user_001",
                "conversation_id": conv["id"] if conv else None,
            })

    def test_finished_turn_is_measured(self):
        res = self.chat(_FakeChatResponse)
        self.assertIn('"done":true', res.text)
        (row,) = self.perf.records()
        self.assertEqual((row["kind"], row["status"], row["model"], row["persona"]),
                         ("chat", "ok", main.OLLAMA_MODEL, "pao"))
        self.assertEqual((row["tokens_per_s"], row["output_tokens"], row["load_s"]), (20.0, 40, 3.0))
        self.assertLessEqual(row["prep_s"], row["first_token_s"])
        self.assertLessEqual(row["first_token_s"], row["total_s"])

    def test_only_numbers_are_written(self):
        self.chat(_FakeChatResponse)
        raw = self.raw_log()
        for private in (SECRET_MESSAGE, "Hừ, chào.", "user_001", "Minh"):
            self.assertNotIn(private, raw)

    def test_ollama_error_is_measured_by_error_name_only(self):
        res = self.chat(_BrokenChatResponse, conversation=False)
        self.assertIn("Ollama không phản hồi", res.text)   # client vẫn nhận lỗi đầy đủ như cũ
        (row,) = self.perf.records()
        self.assertEqual((row["status"], row["error"]), ("error", "RuntimeError"))
        self.assertNotIn("first_token_s", row)
        self.assertNotIn("Ollama không phản hồi", self.raw_log())

    def test_stream_cut_off_is_incomplete(self):
        self.chat(_CutOffChatResponse, conversation=False)
        (row,) = self.perf.records()
        self.assertEqual(row["status"], "incomplete")
        self.assertIn("first_token_s", row)


class ImagePerfTests(_ApiCase):
    def generate(self, *, job=None, quality="Tốt nhất (50 steps)", chat_message=""):
        def fake_job(*args, **kwargs):
            return Image.new("RGB", (512, 768), (240, 160, 190))

        a, b = self.lifespan()
        with (
            a, b,
            patch.object(main, "GENERATED_IMAGES_DIR", str(self.images_dir)),
            patch.object(main, "enrich_for_create_ex", return_value=("1girl, window, letter", True)),
            patch.object(main, "get_checkpoint_model", return_value="anything-v5.safetensors"),
            patch.object(main, "run_comfy_job", side_effect=job or fake_job),
            self.client() as client,
        ):
            return client.post("/api/images/generate", json={
                "prompt": SECRET_PROMPT, "persona_id": "pao", "user_id": "user_001",
                "quality": quality, "upscale": "Không upscale", "include_base64": False,
                "chat_message": chat_message,
            })

    def test_finished_image_is_measured(self):
        res = self.generate()
        self.assertEqual(res.status_code, 200)
        (row,) = self.perf.records()
        self.assertEqual((row["kind"], row["status"], row["quality"], row["upscale"], row["style"]),
                         ("image", "ok", "Tốt nhất (50 steps)", "Không upscale", "Anime"))
        self.assertEqual((row["model"], row["width"], row["height"]), ("anything-v5.safetensors", 512, 768))
        self.assertEqual((row["enrich"], row["from_chat"], row["translation_ok"]), (True, False, True))
        timing = res.json()["timing"]
        self.assertEqual(set(timing), {"translate_s", "comfy_s", "total_s"})
        self.assertEqual(timing["total_s"], row["total_s"])
        self.assertLessEqual(timing["comfy_s"], timing["total_s"])

    def test_prompt_is_not_written(self):
        self.generate(chat_message="vẽ cho anh một tấm em đọc thư")
        raw = self.raw_log()
        for private in (SECRET_PROMPT, "đọc thư", "1girl", "user_001"):
            self.assertNotIn(private, raw)
        self.assertTrue(self.perf.records()[0]["from_chat"])

    def test_failed_and_busy_jobs_are_counted_but_not_timed_as_ok(self):
        def broken(*args, **kwargs):
            raise ComfyUIError("ComfyUI từ chối job (giả lập)")

        def busy(*args, **kwargs):
            raise ResourceBusyError("đang có job khác")

        self.assertEqual(self.generate(job=broken).status_code, 502)
        self.assertEqual(self.generate(job=busy).status_code, 429)
        rows = self.perf.records()
        self.assertEqual([(r["status"], r.get("error")) for r in rows], [("error", "comfy"), ("busy", None)])
        self.assertIn("comfy_s", rows[0])
        preset = self.perf.summarize()["image"]["presets"][0]
        self.assertEqual((preset["failed"], preset["busy"], preset["total_s"]), (1, 1, None))

    def test_stats_endpoint_reports_the_typical_time(self):
        for _ in range(3):
            self.generate()
        a, b = self.lifespan()
        with a, b, self.client() as client:
            res = client.get("/api/stats")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["enabled"])
        (preset,) = body["image"]["presets"]
        self.assertEqual((preset["quality"], preset["ok"], preset["total_s"]["n"]), ("Tốt nhất (50 steps)", 3, 3))
        self.assertEqual(body["chat"], {"turns": 0, "models": []})


class DeleteAllClearsPerfTests(_ApiCase):
    def test_delete_all_data_also_clears_the_perf_log(self):
        # Sổ không có nội dung nhưng có giờ từng lượt chat/vẽ: "xoá toàn bộ" phải xoá luôn.
        small = PerfLog(self.perf.path, max_bytes=300)
        main.perf_log = self.perf = small
        for _ in range(8):
            small.record("chat", status="ok", model="m", total_s=1.0)
        self.assertTrue(small.rotated_path.exists())
        a, b = self.lifespan()
        with a, b, self.client() as client:
            res = client.post("/api/data/delete", json={"user_id": "user_001", "confirm": "XOÁ"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["perf_files_removed"], 2)
        self.assertFalse(small.path.exists())
        self.assertFalse(small.rotated_path.exists())
        self.assertEqual(small.summarize()["measurements"], 0)

    def test_wrong_confirmation_keeps_the_perf_log(self):
        self.perf.record("chat", status="ok", model="m")
        a, b = self.lifespan()
        with a, b, self.client() as client:
            res = client.post("/api/data/delete", json={"user_id": "user_001", "confirm": "co"})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.perf.records()), 1)


if __name__ == "__main__":
    unittest.main()
