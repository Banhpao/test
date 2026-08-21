"""Smoke test REST API với SQLite và Ollama giả; không cần dịch vụ đang chạy."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from yumeai.core import chat_context
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


class ApiStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = YumeDatabase(Path(self.tmp.name) / "api.db")
        self.database.initialize(PERSONAS)
        self.old_database = main.database
        main.database = self.database

    def tearDown(self):
        main.database = self.old_database
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
                    "persona_id": "yuki",
                },
            )
            self.assertEqual(created.status_code, 200)
            conversation_id = created.json()["id"]
            self.database.upsert_memory_fact(
                "user_001", "yuki", "favorite_drink", "cà phê đen"
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
                        "persona_id": "yuki",
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

    def test_user_cannot_read_another_users_conversation(self):
        conversation = self.database.create_conversation("owner", "hana")
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
        history_a = [
            {"role": "user", "content": f"A-{i}"} for i in range(13)
        ]
        history_b = [
            {"role": "user", "content": f"B-{i}"} for i in range(13)
        ]
        chat_context.clear_memory_cache()
        with patch(
            "yumeai.services.ollama_service.summarize_conversation",
            side_effect=["trí nhớ A", "trí nhớ B"],
        ) as summarize:
            summary_a = chat_context.summarize_old_history(history_a, "Yuki")
            summary_b = chat_context.summarize_old_history(history_b, "Yuki")

        self.assertEqual(summary_a, "trí nhớ A")
        self.assertEqual(summary_b, "trí nhớ B")
        self.assertEqual(summarize.call_count, 2)

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
                    "persona_id": "yuki",
                    "value": "cà phê đen",
                },
            )
            listed = client.get(
                "/api/memory",
                params={"user_id": "user_001", "persona_id": "yuki"},
            )
            deleted = client.delete(
                "/api/memory/favorite_drink",
                params={"user_id": "user_001", "persona_id": "yuki"},
            )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(listed.json()["facts"][0]["fact_value"], "cà phê đen")
        self.assertEqual(deleted.json()["deleted"], True)


if __name__ == "__main__":
    unittest.main()
