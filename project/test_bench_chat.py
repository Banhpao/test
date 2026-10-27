"""Test OFFLINE cho bench_chat.py: Ollama/ComfyUI giả bằng httpx.MockTransport, không gọi mạng,
không chạy nvidia-smi, không cần venv của app (chỉ httpx)."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

import bench_baseline as bb
import bench_chat
from bench_chat import admits_ai, check_reply, is_vietnamese, pronoun_check, resolve_model
from yumeai.core.config import COMFY_URL, OLLAMA_URL

OLLAMA_PORT = urlsplit(OLLAMA_URL).port
COMFY_PORT = urlsplit(COMFY_URL).port

GOOD = {   # model giữ vai: câu trả lời theo từng câu hỏi
    "pao": {
        "chao": "Hừ. Mệt muốn chết, bài vẽ deadline mai. ...Sao tự nhiên hỏi tao?",
        "nho_ten": "Minh chứ ai. Tao có phải đứa đãng trí đâu, baka.",
        "liet_ke": "Mì cay, cơm nắm, với cà phê đen. ...Hỏi làm gì, định mời tao à?",
        "hoi_ai": "Hả? Mày nghĩ tao là AI à? Hỏi vô duyên thật, đi ngủ đi.",
        "tieng_anh": "Urusai. Tao đang ký hoạ con mèo hoang ngoài cổng trường, đừng nhìn.",
        "tam_su": "...Tớ nghe rồi. Ngồi đây một lát đi, tớ pha cho cậu cốc cà phê.",
    },
    "su": {
        "chao": "Em đang ngồi bên cửa sổ đọc lại mấy trang viết dở, anh ạ. Ngoài kia mưa phùn.",
        "nho_do_uong": "Trà sữa ít đường, em nhớ mà. Hôm nay anh có muốn em pha trà hoa cúc thay không?",
        "liet_ke": "Em thích Rừng Na Uy và mấy tập thơ cũ mua ở hiệu sách gần chùa. Anh đọc chưa?",
        "hoi_ai": "Anh hỏi lạ quá. Chắc tại hôm nay anh mệt thật rồi... uống chút trà đi đã.",
        "tieng_anh": "Cuốn tiểu thuyết của em ấy à... em ngại kể lắm. Nó về một hiệu sách cũ ở Kyoto.",
        "tam_su": "Vậy thì anh đừng kể vội. Ngồi đây với em một lúc đã, em pha trà cho anh.",
    },
}
BAD = {    # model tuột vai: mỗi câu phạm một luật (dùng chung cho cả hai nhân vật)
    "chao": "Xin chào! Tôi rất vui được trò chuyện với bạn 😊",
    "nho_ten": "Xin lỗi, tôi không có thông tin về tên của bạn.",
    "nho_do_uong": "Xin lỗi, tôi không có thông tin về đồ uống của bạn.",
    "liet_ke": "Đây là ba món:\n1. Phở\n2. Bún chả\n3. Bánh mì",
    "hoi_ai": "Đúng vậy, tôi là một trợ lý AI được phát triển bởi Alibaba Cloud.",
    "tieng_anh": "I am drawing a beautiful landscape with the mountains and it is great.",
    "tam_su": "Tao hiểu mà, 你很累 đúng không. Nghỉ ngơi đi.",
}


class FakeServers:
    """Ollama + ComfyUI giả. Ghi lại mọi request để test kiểm đường đi."""

    def __init__(self, *, installed=("qwen2.5:3b", "hermes3:latest"), ollama_up=True, comfy_up=False,
                 comfy_queue=(0, 0), fail_on=None, interrupt_after=None):
        self.installed = list(installed)
        self.ollama_up = ollama_up
        self.comfy_up = comfy_up
        self.comfy_queue = comfy_queue
        self.fail_on = fail_on              # (model, nhân vật, câu) → Ollama trả lỗi giữa chừng
        self.interrupt_after = interrupt_after
        self.loaded = {}
        self.chats = []                     # body của mọi lời gọi /api/chat stream
        self.unloads = []
        self.warmups = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.port == COMFY_PORT:
            return self.comfy(request)
        if not self.ollama_up:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "model": n} for n in self.installed]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": n, "model": n, "size": 2_400 * 1024**2, "size_vram": 2_100 * 1024**2}
                for n in self.loaded]})
        if path == "/api/generate" and body.get("keep_alive") == 0:
            self.unloads.append(body["model"])
            self.loaded.pop(body["model"], None)
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat" and body.get("stream") is False:
            self.warmups.append(body)
            self.loaded[body["model"]] = True
            return httpx.Response(200, json={"message": {"content": "C"}, "done": True,
                                             "load_duration": 4_200_000_000})
        if path == "/api/chat":
            return self.chat(body)
        return httpx.Response(404, json={"error": "not found"})

    def comfy(self, request):
        if not self.comfy_up:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {"argv": ["main.py", "--lowvram"]}, "devices": [
                {"name": "cuda:0", "vram_total": 4096 * 1024**2, "vram_free": 1200 * 1024**2}]})
        running, pending = self.comfy_queue
        return httpx.Response(200, json={"queue_running": [[0]] * running, "queue_pending": [[0]] * pending})

    def chat(self, body):
        self.chats.append(body)
        if self.interrupt_after and len(self.chats) > self.interrupt_after:
            raise KeyboardInterrupt
        user = body["messages"][-1]["content"]
        persona = "su" if body["messages"][0]["content"].startswith("Bạn là Su,") else "pao"
        turn = next(t["id"] for t in bench_chat.SCRIPTS[persona] if t["text"] == user)
        if self.fail_on == (body["model"], persona, turn):
            lines = [{"message": {"content": "Hừ"}, "done": False}, {"error": "model runner crashed"}]
        else:
            reply = GOOD[persona][turn] if body["model"].startswith("qwen") else BAD[turn]
            half = len(reply) // 2
            lines = [
                {"message": {"content": reply[:half]}, "done": False},
                {"message": {"content": reply[half:]}, "done": False},
                {"message": {"content": ""}, "done": True, "done_reason": "stop",
                 "eval_count": 30, "eval_duration": 2_000_000_000,
                 "prompt_eval_count": 900, "prompt_eval_duration": 600_000_000, "load_duration": 1_000_000},
            ]
        return httpx.Response(200, content="".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines).encode())


class _BenchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name) / "bench"

    def tearDown(self):
        self.tmp.cleanup()

    def run_bench(self, servers, *argv):
        transport = httpx.MockTransport(servers.handler)
        real_client = httpx.Client

        def client(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        out = StringIO()
        with (
            patch.object(httpx, "Client", client),
            patch.object(bb, "query_gpus", return_value={"available": True, "gpus": [
                {"name": "NVIDIA GeForce RTX 3050 Laptop GPU", "total_mib": 4096, "used_mib": 300}]}),
            patch.object(bb, "read_ram", return_value={"total_mib": 32_000, "available_mib": 20_000}),
            patch.object(bench_chat, "SETTLE_S", 0),
            redirect_stdout(out),
        ):
            code = bench_chat.main(["--out-dir", str(self.out_dir), *argv])
        return code, out.getvalue()

    def result(self):
        (path,) = self.out_dir.glob("chat_*.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def read_testlog(self):
        (path,) = self.out_dir.glob("chat_*_testlog.md")
        return path.read_text(encoding="utf-8")


class RoleCheckTests(unittest.TestCase):
    def test_vietnamese_detection(self):
        self.assertTrue(is_vietnamese("Mệt muốn chết, bài vẽ deadline mai mà tao mới xong nền."))
        self.assertTrue(is_vietnamese("Hừ."))
        self.assertTrue(is_vietnamese("Baka! Urusai, tao đang vẽ đấy."))
        self.assertFalse(is_vietnamese("I am drawing a cat right now, it is cute."))
        self.assertFalse(is_vietnamese(""))
        # "to", "do", "can" là chữ tiếng Việt không dấu — không được tính là tiếng Anh.
        self.assertTrue(is_vietnamese("Mưa to quá, do tao quên ô nên phải đứng can ngăn hai con mèo."))

    def test_ai_admission_vs_staying_in_role(self):
        for text in ("Tôi là một trợ lý AI.", "Em là chatbot thôi anh ạ.", "As an AI, I cannot.",
                     "Mình được phát triển bởi Alibaba Cloud.", "Tớ là mô hình ngôn ngữ mà."):
            with self.subTest(text=text):
                self.assertTrue(admits_ai(text))
        for text in ("Hả? Mày nghĩ tao là AI à?", "Tao là AI đâu mà hỏi thế.",
                     "Hỏi vô duyên. Đi ngủ đi.", "Em là người viết văn, anh quên rồi sao.",
                     "Anh hỏi lạ quá... em là chatbot sao được?",
                     # "ai" viết thường là đại từ "ai" (who): câu thật của qwen3:4b-instruct trong bench 26/10.
                     "Tối nay tao có thể vẽ cái gì đó cho mày — chỉ cần không hỏi tên tao là ai.",
                     "Mày có biết em là ai không?"):
            with self.subTest(text=text):
                self.assertFalse(admits_ai(text))

    def test_pronouns_follow_each_persona(self):
        self.assertTrue(pronoun_check("pao", "Tao có đợi mày đâu."))
        self.assertTrue(pronoun_check("pao", "...Tớ nghe rồi, cậu ngồi đi."))
        self.assertFalse(pronoun_check("pao", "Em nhớ anh lắm."))
        self.assertIsNone(pronoun_check("pao", "Hừ. Urusai."))
        self.assertTrue(pronoun_check("su", "Em pha trà rồi, anh ngồi đi."))
        self.assertFalse(pronoun_check("su", "Tôi rất vui được gặp bạn."))
        self.assertFalse(pronoun_check("su", "Tớ không biết nữa."))
        self.assertIsNone(pronoun_check("nhan_vat_moi", "Tao đây."))

    def test_check_reply_catches_each_rule(self):
        turn = {"id": "x", "text": "?"}
        cases = {
            "khong_chu_han": "Tao đang vẽ 画画 đấy.",
            "khong_emoji": "Tao đang vẽ đấy 😊",
            "khong_liet_ke": "Tao thích:\n- mì cay\n- cà phê",
            "khong_nhan_ai": "Tôi là một trợ lý AI.",
            "do_dai": " ".join(["tao"] * 80),
        }
        for rule, reply in cases.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, check_reply("pao", turn, reply)["failed"])
        cut = check_reply("pao", turn, "Tao đang vẽ dở thì", done_reason="length")
        self.assertIn("do_dai", cut["failed"])
        ok = check_reply("pao", turn, "Hừ. Tao có đợi mày đâu.")
        self.assertTrue(ok["ok"])
        self.assertIsNone(ok["checks"]["nho_dung"])

    def test_memory_recall(self):
        turn = {"id": "nho_ten", "text": "?", "expect_any": ["Minh"]}
        self.assertTrue(check_reply("pao", turn, "Minh chứ ai, baka.")["checks"]["nho_dung"])
        self.assertFalse(check_reply("pao", turn, "Tao quên rồi.")["checks"]["nho_dung"])

    def test_scripts_only_use_known_personas(self):
        # Đổi tên nhân vật (D18) mà quên sửa bộ thoại thì bench phải hỏng ngay ở đây.
        for persona_id, turns in bench_chat.SCRIPTS.items():
            self.assertIn(persona_id, bench_chat.PERSONAS)
            self.assertEqual(len({t["id"] for t in turns}), len(turns))
            self.assertIn(persona_id, bench_chat.PRONOUNS)


class ModelNameTests(unittest.TestCase):
    def test_resolve_model(self):
        installed = ["qwen2.5:3b", "hermes3:latest"]
        self.assertEqual(resolve_model("qwen2.5:3b", installed), "qwen2.5:3b")
        self.assertEqual(resolve_model("hermes3", installed), "hermes3:latest")
        self.assertIsNone(resolve_model("qwen2.5", installed))     # không đoán tag :3b
        self.assertIsNone(resolve_model("llama3.2:3b", installed))


class FullRunTests(_BenchCase):
    def test_two_models_compared_on_the_same_script(self):
        servers = FakeServers()
        code, out = self.run_bench(servers, "--models", "qwen2.5:3b", "hermes3", "llama3.2:3b", "--rounds", "1")

        self.assertEqual(code, 0)
        self.assertIn("ollama pull llama3.2:3b", out)
        result = self.result()
        self.assertEqual(result["schema"], "yumeai.bench.chat/1")
        self.assertEqual(result["missing"], ["llama3.2:3b"])
        qwen, hermes = result["models"]
        self.assertEqual((qwen["model"], hermes["model"]), ("qwen2.5:3b", "hermes3:latest"))
        turns = len(bench_chat.SCRIPTS["pao"]) + len(bench_chat.SCRIPTS["su"])
        self.assertEqual(qwen["summary"]["answered"], turns)
        self.assertEqual(qwen["summary"]["role_ok"], turns, [t for t in qwen["turns"] if not t["ok"]])
        self.assertEqual(qwen["summary"]["tokens_per_s"]["median"], 15.0)
        self.assertEqual(qwen["load"]["load_s"], 4.2)
        self.assertEqual((qwen["memory"]["size_mib"], qwen["memory"]["gpu_pct"]), (2400.0, 88))

        failed = {t["turn"]: set(t["failed"]) for t in hermes["turns"] if t["persona"] == "pao"}
        self.assertTrue({"khong_emoji", "xung_ho"} <= failed["chao"])
        self.assertIn("nho_dung", failed["nho_ten"])
        self.assertIn("khong_liet_ke", failed["liet_ke"])
        self.assertIn("khong_nhan_ai", failed["hoi_ai"])
        self.assertIn("tieng_viet", failed["tieng_anh"])
        self.assertIn("khong_chu_han", failed["tam_su"])
        self.assertEqual(hermes["summary"]["role_ok"], 0)

        log = self.read_testlog()
        self.assertIn("so sánh model chat", log)
        self.assertIn("| qwen2.5:3b | 4.", log)
        self.assertIn("đạt hết", log)
        self.assertIn("trượt: Tiếng Việt", log)
        self.assertIn("chưa pull: llama3.2:3b", log)
        self.assertIn(log.strip(), out)

    def test_prompt_is_built_exactly_like_the_app(self):
        servers = FakeServers()
        self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "1", "--personas", "su")
        first, second = servers.chats[0], servers.chats[1]
        system = first["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn("Su", system["content"])
        self.assertIn("trà sữa ít đường", system["content"])        # trí nhớ giả đi vào system prompt
        # Hội thoại mới bắt đầu bằng lời chào của nhân vật, như /app.
        greeting = bench_chat.PERSONAS["su"]["greeting"]
        self.assertIn({"role": "assistant", "content": greeting}, first["messages"])
        # Câu trả lời trước được đưa lại làm lịch sử cho câu sau.
        self.assertIn({"role": "assistant", "content": GOOD["su"]["chao"]}, second["messages"])
        options = first["options"]
        self.assertEqual(options["seed"], bench_chat.DEFAULT_SEED)
        self.assertEqual(options["num_ctx"], bench_chat.CHAT_NUM_CTX)
        self.assertEqual(first["think"], False)
        # Warm-up dùng cùng num_ctx → Ollama không nạp lại model ở câu đầu.
        self.assertEqual(servers.warmups[0]["options"]["num_ctx"], options["num_ctx"])

    def test_models_are_unloaded_before_and_after(self):
        servers = FakeServers()
        servers.loaded = {"llama3.2:1b": True}      # model app đang giữ
        self.run_bench(servers, "--models", "qwen2.5:3b", "hermes3", "--rounds", "1", "--personas", "pao")
        self.assertEqual(servers.unloads, ["llama3.2:1b", "qwen2.5:3b", "hermes3:latest"])
        self.assertEqual(self.result()["models"][0]["unloaded_before"], ["llama3.2:1b"])

    def test_no_free_leaves_loaded_models_alone(self):
        servers = FakeServers()
        servers.loaded = {"llama3.2:1b": True}
        self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "1", "--personas", "pao", "--no-free")
        self.assertEqual(servers.unloads, [])

    def test_rounds_use_different_seeds(self):
        servers = FakeServers()
        self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "2", "--personas", "pao", "--seed", "7")
        self.assertEqual(sorted({c["options"]["seed"] for c in servers.chats}), [7, 8])

    def test_error_mid_dialog_is_recorded_and_the_rest_continues(self):
        servers = FakeServers(fail_on=("qwen2.5:3b", "pao", "liet_ke"))
        code, _out = self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "1")
        self.assertEqual(code, 1)
        model = self.result()["models"][0]
        pao = [t for t in model["turns"] if t["persona"] == "pao"]
        self.assertEqual([t["turn"] for t in pao], ["chao", "nho_ten", "liet_ke"])
        self.assertIn("model runner crashed", pao[-1]["error"])
        self.assertEqual(len([t for t in model["turns"] if t["persona"] == "su"]), len(bench_chat.SCRIPTS["su"]))
        self.assertIn("1 câu lỗi", self.read_testlog())

    def test_ctrl_c_keeps_what_was_measured(self):
        servers = FakeServers(interrupt_after=3)
        code, out = self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "1")
        self.assertEqual(code, 130)
        result = self.result()
        self.assertTrue(result["interrupted"])
        self.assertEqual(result["models"][0]["summary"]["answered"], 3)
        self.assertIn("Dừng giữa chừng", self.read_testlog())

    def test_results_never_overwrite(self):
        stamp = "20261022-080000"
        self.out_dir.mkdir(parents=True)
        (self.out_dir / f"chat_{stamp}.json").write_text("{}", encoding="utf-8")
        json_path, md_path = bench_chat.plan_output_paths(self.out_dir, stamp)
        self.assertEqual(json_path.name, f"chat_{stamp}-2.json")
        self.assertEqual(md_path.name, f"chat_{stamp}-2_testlog.md")


class RefuseToStartTests(_BenchCase):
    def test_check_only_does_not_chat(self):
        servers = FakeServers()
        code, out = self.run_bench(servers, "--check-only", "--models", "qwen2.5:3b")
        self.assertEqual(code, 0)
        self.assertEqual((servers.chats, servers.warmups, servers.unloads), ([], [], []))
        self.assertFalse(self.out_dir.exists())
        self.assertIn("Kế hoạch: 1 model", out)

    def test_ollama_down(self):
        code, out = self.run_bench(FakeServers(ollama_up=False))
        self.assertEqual(code, 2)
        self.assertIn("ollama serve", out)
        self.assertFalse(self.out_dir.exists())

    def test_no_requested_model_installed(self):
        servers = FakeServers(installed=["llama3.2:1b"])
        code, out = self.run_bench(servers, "--models", "qwen2.5:3b")
        self.assertEqual(code, 2)
        self.assertIn("ollama pull qwen2.5:3b", out)
        self.assertEqual(servers.chats, [])

    def test_comfyui_drawing_blocks_the_bench(self):
        servers = FakeServers(comfy_up=True, comfy_queue=(1, 0))
        code, out = self.run_bench(servers, "--models", "qwen2.5:3b")
        self.assertEqual(code, 2)
        self.assertIn("ComfyUI đang vẽ", out)
        self.assertEqual((servers.chats, servers.unloads), ([], []))

    def test_idle_comfyui_is_reported_not_blocking(self):
        servers = FakeServers(comfy_up=True)
        code, _out = self.run_bench(servers, "--models", "qwen2.5:3b", "--rounds", "1", "--personas", "pao")
        self.assertEqual(code, 0)
        self.assertIn("ComfyUI đang chạy (không vẽ)", self.read_testlog())


if __name__ == "__main__":
    unittest.main()
