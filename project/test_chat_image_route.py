"""Test offline cho việc xin ảnh ngay trong khung chat của /app (D19).

Không cần Ollama hay ComfyUI: bộ vẽ được thay bằng hàm trả ảnh giả. Kiểm hai phần:
  - POST /api/chat/intent phân biệt "trò chuyện" với "xin ảnh" bằng plan_request()
    dùng chung với Gradio;
  - POST /api/images/generate kèm chat_message lưu câu xin ảnh + ảnh vào hội thoại,
    và KHÔNG lưu gì nếu vẽ lỗi.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import main
from yumeai.core import prompt_router
from yumeai.core.perf import PerfLog
from yumeai.core.personas import PERSONAS
from yumeai.services.comfy_service import ComfyUIError
from yumeai.storage import YumeDatabase


class _ApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = YumeDatabase(Path(self.tmp.name) / "api.db")
        self.database.initialize(PERSONAS)
        self.images_dir = Path(self.tmp.name) / "images"
        self.images_dir.mkdir()
        self.old_database = main.database
        main.database = self.database
        # Số đo hiệu năng của test không được rơi vào data/perf.jsonl thật.
        self.old_perf_log = main.perf_log
        main.perf_log = PerfLog(Path(self.tmp.name) / "perf.jsonl")

    def tearDown(self):
        main.database = self.old_database
        main.perf_log = self.old_perf_log
        self.tmp.cleanup()

    def client(self):
        return TestClient(main.app)

    def lifespan_patches(self):
        return (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
        )


class ChatIntentTests(_ApiCase):
    def intent(self, message, **extra):
        a, b = self.lifespan_patches()
        # Không có Ollama: planner LLM coi như lỗi → dùng đúng kết quả rule-based.
        with a, b, patch.object(prompt_router, "_llm_plan_request", return_value=None), self.client() as client:
            return client.post("/api/chat/intent", json={"message": message, "persona_id": "pao", **extra})

    def test_requests_for_a_picture_are_text2img(self):
        for message in (
            "vẽ cho anh một tấm em ngồi bên cửa sổ lúc mưa đêm",
            "chụp cho anh tấm hình em đang cầm cà phê đi",
            "gửi ảnh đi em",
            "tạo ảnh em ở bờ sông",
        ):
            with self.subTest(message=message):
                res = self.intent(message)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["task"], "text2img")

    def test_ordinary_chat_stays_chat(self):
        for message in (
            "hôm nay em thế nào?",
            "hình như em buồn",
            "tạo động lực cho anh đi",
            "anh nhớ em",
            "màn hình máy anh hỏng rồi",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.intent(message).json()["task"], "chat")

    def test_style_words_in_the_request_win(self):
        res = self.intent("vẽ em kiểu chân thực ở quán cà phê", style="Anime")
        self.assertEqual(res.json()["style"], "Realistic")
        self.assertEqual(self.intent("vẽ em ở quán cà phê", style="Semi").json()["style"], "Semi")

    def test_edit_tasks_fall_back_to_chat_without_an_image(self):
        plan = prompt_router.PromptPlan(task="inpaint", reason="giả lập planner").to_dict()
        a, b = self.lifespan_patches()
        with a, b, patch.object(main, "plan_request", return_value=plan), self.client() as client:
            res = client.post("/api/chat/intent", json={"message": "xoá cái cây đi", "persona_id": "pao"})
        self.assertEqual(res.json()["task"], "chat")

    def test_old_persona_id_empty_message_and_unknown_persona(self):
        self.assertEqual(self.intent("vẽ em đi", persona_id="yuki").status_code, 200)
        self.assertEqual(self.intent("   ").status_code, 400)
        self.assertEqual(self.intent("vẽ em đi", persona_id="khong_co").status_code, 404)


class GenerateFromChatTests(_ApiCase):
    def generate(self, conversation_id, *, chat_message="", fail=False):
        def fake_job(*args, **kwargs):
            if fail:
                raise ComfyUIError("ComfyUI từ chối job (giả lập)")
            return Image.new("RGB", (64, 96), (240, 160, 190))

        a, b = self.lifespan_patches()
        with (
            a, b,
            patch.object(main, "GENERATED_IMAGES_DIR", str(self.images_dir)),
            patch.object(main, "enrich_for_create_ex", return_value=("1girl, window, rain", True)),
            patch.object(main, "get_checkpoint_model", return_value="anything-v5.safetensors"),
            patch.object(main, "run_comfy_job", side_effect=fake_job),
            self.client() as client,
        ):
            return client.post("/api/images/generate", json={
                "prompt": chat_message or "Pao bên cửa sổ",
                "persona_id": "pao",
                "user_id": "user_001",
                "conversation_id": conversation_id,
                "chat_message": chat_message,
                "include_base64": False,
            })

    def test_chat_message_is_saved_before_the_image(self):
        conv = self.database.create_conversation("user_001", "pao")
        res = self.generate(conv["id"], chat_message="vẽ cho anh một tấm em bên cửa sổ")
        self.assertEqual(res.status_code, 200)

        messages = self.database.list_messages(conv["id"])
        self.assertEqual([(m["role"], m["content_type"]) for m in messages], [("user", "text"), ("assistant", "image")])
        self.assertEqual(messages[0]["content"], "vẽ cho anh một tấm em bên cửa sổ")
        image = json.loads(messages[1]["content"])
        self.assertEqual(image["image_url"], res.json()["image_url"])
        self.assertTrue((self.images_dir / Path(image["image_url"]).name).exists())

    def test_without_chat_message_only_the_image_is_saved(self):
        conv = self.database.create_conversation("user_001", "pao")
        self.assertEqual(self.generate(conv["id"]).status_code, 200)
        messages = self.database.list_messages(conv["id"])
        self.assertEqual([(m["role"], m["content_type"]) for m in messages], [("assistant", "image")])

    def test_failed_drawing_saves_nothing(self):
        conv = self.database.create_conversation("user_001", "pao")
        res = self.generate(conv["id"], chat_message="vẽ cho anh một tấm", fail=True)
        self.assertEqual(res.status_code, 502)
        self.assertEqual(self.database.list_messages(conv["id"]), [])
        self.assertEqual(self.database.list_images(user_id="user_001"), [])


if __name__ == "__main__":
    unittest.main()
