"""Smoke test REST API với SQLite và Ollama giả; không cần dịch vụ đang chạy."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from yumeai.core import chat_context
from yumeai.core.chat_context import history_load_limit
from yumeai.core.perf import PerfLog
from yumeai.core.personas import PERSONAS
from yumeai.storage import YumeDatabase


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
        yield json.dumps({"message": {"content": ""}, "done": True})


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        return _FakeChatResponse()


class _BrokenChatResponse:
    """Ollama chết giữa chừng: mở stream là lỗi."""

    async def __aenter__(self):
        raise RuntimeError("Ollama không phản hồi")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _BrokenAsyncClient(_FakeAsyncClient):
    def stream(self, *args, **kwargs):
        return _BrokenChatResponse()


class ApiStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = YumeDatabase(Path(self.tmp.name) / "api.db")
        self.database.initialize(PERSONAS)
        self.old_database = main.database
        main.database = self.database
        # Số đo hiệu năng của test không được rơi vào data/perf.jsonl thật.
        self.old_perf_log = main.perf_log
        main.perf_log = PerfLog(Path(self.tmp.name) / "perf.jsonl")

    def tearDown(self):
        main.database = self.old_database
        main.perf_log = self.old_perf_log
        self.tmp.cleanup()

    def test_conversation_chat_and_reload(self):
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            created = client.post(
                "/api/conversations",
                json={
                    "user_id": "user_001",
                    "user_name": "Minh",
                    "persona_id": "pao",
                },
            )
            self.assertEqual(created.status_code, 200)
            conversation_id = created.json()["id"]
            self.database.upsert_memory_fact(
                "user_001", "pao", "favorite_drink", "cà phê đen"
            )

            real_builder = main.build_persona_messages
            with (
                patch.object(main.httpx, "AsyncClient", _FakeAsyncClient),
                patch.object(
                    main, "build_persona_messages", wraps=real_builder
                ) as builder,
            ):
                response = client.post(
                    "/api/chat",
                    json={
                        "message": "Chào em",
                        "persona_id": "pao",
                        "user_id": "user_001",
                        "conversation_id": conversation_id,
                    },
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn('"token": "Hừ, "', response.text)
            self.assertIn(
                "favorite_drink: cà phê đen",
                builder.call_args.kwargs["memory_summary"],
            )
            self.assertIn("YumeAI", client.get("/app").text)
            loaded = client.get(
                f"/api/conversations/{conversation_id}",
                params={"user_id": "user_001"},
            )
            roles = [item["role"] for item in loaded.json()["messages"]]
            self.assertEqual(roles, ["assistant", "user", "assistant"])
            self.assertEqual(loaded.json()["messages"][-1]["content"], "Hừ, chào.")

    def _chat(self, client, conversation_id, message="Chào em"):
        return client.post(
            "/api/chat",
            json={
                "message": message,
                "persona_id": "pao",
                "user_id": "user_001",
                "conversation_id": conversation_id,
            },
        )

    def _roles(self, client, conversation_id):
        loaded = client.get(
            f"/api/conversations/{conversation_id}", params={"user_id": "user_001"}
        )
        return [item["role"] for item in loaded.json()["messages"]]

    def test_failed_reply_leaves_no_orphan_user_message(self):
        """Bản cũ ghi tin user TRƯỚC khi gọi LLM: lỗi là mồ côi, gửi lại là nhân đôi."""
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            conversation_id = client.post(
                "/api/conversations",
                json={"user_id": "user_001", "persona_id": "pao"},
            ).json()["id"]
            with patch.object(main.httpx, "AsyncClient", _BrokenAsyncClient):
                failed = self._chat(client, conversation_id)
            self.assertIn('"error"', failed.text)
            self.assertEqual(self._roles(client, conversation_id), ["assistant"])

            with patch.object(main.httpx, "AsyncClient", _FakeAsyncClient):
                self._chat(client, conversation_id)
            self.assertEqual(
                self._roles(client, conversation_id), ["assistant", "user", "assistant"]
            )

    def test_chat_reads_only_the_needed_history_window(self):
        conversation = self.database.create_conversation("user_001", "pao")
        for index in range(100):
            role = "user" if index % 2 == 0 else "assistant"
            self.database.append_message(conversation["id"], role, f"câu {index}")
        real_builder = main.build_persona_messages
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            patch.object(main, "refresh_conversation_memory", return_value=None),
            patch(
                "yumeai.services.ollama_service.summarize_conversation",
                return_value="",
            ),
            TestClient(main.app) as client,
            patch.object(main.httpx, "AsyncClient", _FakeAsyncClient),
            patch.object(main, "build_persona_messages", wraps=real_builder) as builder,
        ):
            self._chat(client, conversation["id"], "câu mới")
        history = builder.call_args.args[1]
        self.assertEqual(len(history), history_load_limit(100))
        self.assertLess(len(history), 100)
        self.assertEqual(history[-1]["content"], "câu 99")

    def test_other_websites_cannot_read_the_api(self):
        """Bind 127.0.0.1 không chặn được trang web khác trong cùng trình duyệt;
        CORS "*" từng cho trang đó đọc hết hội thoại, trí nhớ và gallery."""
        evil = {"Origin": "https://evil.example"}
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            simple = client.get(
                "/api/conversations", params={"user_id": "user_001"}, headers=evil
            )
            preflight = client.options(
                "/api/chat",
                headers={
                    **evil,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        self.assertEqual(simple.status_code, 200)
        self.assertNotIn("access-control-allow-origin", simple.headers)
        self.assertNotIn("access-control-allow-origin", preflight.headers)

    def test_user_cannot_read_another_users_conversation(self):
        conversation = self.database.create_conversation("owner", "su")
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            response = client.get(
                f"/api/conversations/{conversation['id']}",
                params={"user_id": "someone_else"},
            )
        self.assertEqual(response.status_code, 404)

    def test_summary_cache_does_not_leak_between_conversations(self):
        # Đủ dài để có ĐÚNG một lô tràn khỏi cửa sổ gần (D13): ngắn hơn thì
        # summarize_old_history() chưa tóm tắt gì và test không kiểm được cache.
        # (Bản trước dùng 13 message — từ D13 là chưa đủ một lô nên summary rỗng.)
        count = chat_context.CHAT_HISTORY_MAX_MESSAGES + chat_context.CHAT_SUMMARY_EVERY
        history_a = [
            {"role": "user", "content": f"A-{i}"} for i in range(count)
        ]
        history_b = [
            {"role": "user", "content": f"B-{i}"} for i in range(count)
        ]
        chat_context.clear_memory_cache()
        with patch.object(chat_context, "CHAT_SUMMARY", True), patch(
            # Tóm tắt mặc định tắt từ D29; test này kiểm cache khi nó được bật.
            "yumeai.services.ollama_service.summarize_conversation",
            side_effect=["trí nhớ A", "trí nhớ B"],
        ) as summarize:
            summary_a = chat_context.summarize_old_history(history_a, "Pao")
            summary_b = chat_context.summarize_old_history(history_b, "Pao")
            # Gọi lại hội thoại A: trúng cache của A, không gọi LLM, không lấy của B.
            summary_a_again = chat_context.summarize_old_history(history_a, "Pao")

        self.assertEqual(summary_a, "trí nhớ A")
        self.assertEqual(summary_b, "trí nhớ B")
        self.assertEqual(summary_a_again, "trí nhớ A")
        self.assertEqual(summarize.call_count, 2)
        sent_a, sent_b = (call.args[0] for call in summarize.call_args_list)
        self.assertTrue(sent_a and all(m["content"].startswith("A-") for m in sent_a))
        self.assertTrue(sent_b and all(m["content"].startswith("B-") for m in sent_b))

    def test_memory_api_can_add_list_and_delete_fact(self):
        with (
            patch.object(main, "check_chat_model", return_value=None),
            patch.object(main, "warmup_ollama", return_value=None),
            TestClient(main.app) as client,
        ):
            saved = client.put(
                "/api/memory/favorite_drink",
                json={
                    "user_id": "user_001",
                    "persona_id": "pao",
                    "value": "cà phê đen",
                },
            )
            listed = client.get(
                "/api/memory",
                params={"user_id": "user_001", "persona_id": "pao"},
            )
            deleted = client.delete(
                "/api/memory/favorite_drink",
                params={"user_id": "user_001", "persona_id": "pao"},
            )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(listed.json()["facts"][0]["fact_value"], "cà phê đen")
        # Nhãn tiếng Việt cho bảng ký ức (D25) — UI không còn in khoá tiếng Anh.
        self.assertEqual(listed.json()["labels"]["favorite_food_drink"], "Món / đồ uống yêu thích")
        self.assertEqual(deleted.json()["deleted"], True)


if __name__ == "__main__":
    unittest.main()
