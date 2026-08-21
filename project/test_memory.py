"""Test offline cho memory extraction; không gọi Ollama thật."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yumeai.services.memory_service import (
    MemoryExtractionResult,
    format_memory_context,
    parse_memory_response,
    refresh_conversation_memory,
)
from yumeai.storage import YumeDatabase


PERSONAS = {"yuki": {"name": "Yuki"}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = YumeDatabase(Path(self.tmp.name) / "memory.db")
        self.db.initialize(PERSONAS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parser_is_strict_and_blocks_secrets(self):
        raw = json.dumps({"facts": [
            {"key": "favorite drink", "value": "cà phê đen", "confidence": 1.2},
            {"key": "bank_account", "value": "123456", "confidence": 1},
            {"key": "shopping_preference", "value": "mua sách cũ", "confidence": 0.8},
        ]})

        result = parse_memory_response(raw)

        self.assertTrue(result.success)
        self.assertEqual(result.facts, [
            {
                "key": "favorite_drink",
                "value": "cà phê đen",
                "confidence": 1.0,
            },
            {
                "key": "shopping_preference",
                "value": "mua sách cũ",
                "confidence": 0.8,
            },
        ])

    def test_invalid_json_is_retryable_failure(self):
        result = parse_memory_response("không phải json")
        self.assertFalse(result.success)
        self.assertEqual(result.facts, [])

    def test_batch_refresh_upserts_fact_and_advances_cursor(self):
        conversation = self.db.create_conversation("user_001", "yuki")
        for index in range(12):
            role = "user" if index % 2 == 0 else "assistant"
            self.db.append_message(
                conversation["id"], role, f"message {index}"
            )
        extracted = MemoryExtractionResult(True, [{
            "key": "favorite_drink",
            "value": "cà phê đen",
            "confidence": 0.95,
        }])

        with patch(
            "yumeai.services.memory_service.extract_memory_facts",
            return_value=extracted,
        ):
            result = refresh_conversation_memory(self.db, conversation["id"])

        self.assertEqual(result, {"status": "processed", "facts": 1})
        facts = self.db.list_memory_facts("user_001", "yuki")
        self.assertEqual(facts[0]["fact_value"], "cà phê đen")
        self.assertEqual(self.db.pending_memory_messages(conversation["id"]), [])
        self.assertIn("favorite_drink: cà phê đen", format_memory_context(facts))


if __name__ == "__main__":
    unittest.main()
