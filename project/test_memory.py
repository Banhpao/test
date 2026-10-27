"""Test offline cho memory extraction; không gọi Ollama thật."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yumeai.core.config import CHAT_NUM_CTX
from yumeai.services import memory_service
from yumeai.services.memory_service import (
    MemoryExtractionResult,
    build_memory_transcript,
    extract_memory_facts,
    format_memory_context,
    parse_memory_response,
    refresh_conversation_memory,
)
from yumeai.storage import YumeDatabase


PERSONAS = {"pao": {"name": "Pao"}}


class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": self._content}}


class _FakeClient:
    """httpx.Client giả, ghi lại payload gửi Ollama."""
    payloads = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, **kwargs):
        _FakeClient.payloads.append(json)
        return _FakeResponse('{"facts": []}')


class _HttpResponse:
    """Response giả có status_code/text như httpx.Response."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")

    def json(self):
        return json.loads(self.text)


_OLD_OLLAMA_PAYLOADS: list = []


class _OldOllamaClient(_FakeClient):
    """Ollama < 0.5: format chỉ nhận "json", gửi JSON schema là HTTP 400."""

    def post(self, url, json=None, **kwargs):
        _OLD_OLLAMA_PAYLOADS.append(json)
        if json.get("format") != "json":
            return _HttpResponse(400, {"error": "json: cannot unmarshal object into Go "
                                                "struct field ChatRequest.format of type string"})
        return _HttpResponse(200, {"message": {"content": (
            '{"facts": [{"key": "name", "value": "Páo", "confidence": 0.9}]}')}})


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
        conversation = self.db.create_conversation("user_001", "pao")
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
        facts = self.db.list_memory_facts("user_001", "pao")
        self.assertEqual(facts[0]["fact_value"], "cà phê đen")
        self.assertEqual(self.db.pending_memory_messages(conversation["id"]), [])
        self.assertIn("favorite_drink: cà phê đen", format_memory_context(facts))

    def _fill(self, count):
        conversation = self.db.create_conversation("user_001", "pao")
        for index in range(count):
            role = "user" if index % 2 == 0 else "assistant"
            self.db.append_message(conversation["id"], role, f"message {index}")
        return conversation

    def test_transcript_keeps_newest_lines_on_message_boundaries(self):
        """Bản cũ cắt [:7000] — giữ phần CŨ NHẤT, vứt phần mới nhất của lô."""
        messages = [
            {"role": "user", "content": f"câu {i} " + "x" * 90} for i in range(30)
        ]
        transcript = build_memory_transcript(messages, budget_chars=1000)
        self.assertLessEqual(len(transcript), 1000)
        self.assertIn("câu 29 ", transcript)
        self.assertNotIn("câu 0 ", transcript)
        # Không có dòng nào bị cắt giữa chừng.
        for line in transcript.splitlines():
            self.assertTrue(line.startswith("user: câu ") and line.endswith("x" * 90))

    def test_extraction_request_matches_chat_context_and_has_room_for_json(self):
        """num_ctx khác chat là Ollama nạp lại model; 320 token thì JSON bị cắt cụt."""
        _FakeClient.payloads = []
        messages = [{"role": "user", "content": "em tên là Páo"}]
        with patch.object(memory_service.httpx, "Client", _FakeClient):
            result = extract_memory_facts(messages)
        self.assertTrue(result.success)
        payload = _FakeClient.payloads[-1]
        self.assertEqual(payload["options"]["num_ctx"], CHAT_NUM_CTX)
        self.assertGreaterEqual(payload["options"]["num_predict"], 600)
        self.assertIsInstance(payload["format"], dict, "phải gửi JSON schema, không chỉ 'json'")
        self.assertIn("facts", payload["format"]["properties"])
        self.assertIs(payload["think"], False)

    def test_old_ollama_rejecting_schema_retries_once_with_plain_json(self):
        """Ollama < 0.5 trả 400 cho format là JSON schema — bản cũ không bao giờ trích được fact."""
        _OLD_OLLAMA_PAYLOADS.clear()
        messages = [{"role": "user", "content": "em tên là Páo"}]
        with patch.object(memory_service.httpx, "Client", _OldOllamaClient):
            result = extract_memory_facts(messages)
        self.assertTrue(result.success, result.error)
        self.assertEqual([f["value"] for f in result.facts], ["Páo"])
        formats = [p["format"] for p in _OLD_OLLAMA_PAYLOADS]
        self.assertEqual(len(formats), 2, "chỉ thử lại đúng một lần")
        self.assertIsInstance(formats[0], dict)
        self.assertEqual(formats[1], "json")

    def test_other_http_400_is_not_retried(self):
        """400 không liên quan tới format (vd model không tồn tại) thì báo lỗi luôn."""
        calls = []

        class _Client(_FakeClient):
            def post(self, url, json=None, **kwargs):
                calls.append(json)
                return _HttpResponse(400, {"error": "model 'x' not found"})

        with patch.object(memory_service.httpx, "Client", _Client):
            result = extract_memory_facts([{"role": "user", "content": "chào"}])
        self.assertFalse(result.success)
        self.assertEqual(len(calls), 1)

    def test_long_batch_fits_the_context_window(self):
        _FakeClient.payloads = []
        messages = [
            {"role": "user", "content": "chuyện dài " + "y" * 900} for _ in range(24)
        ]
        with patch.object(memory_service.httpx, "Client", _FakeClient):
            extract_memory_facts(messages)
        payload = _FakeClient.payloads[-1]
        prompt_chars = sum(len(m["content"]) for m in payload["messages"])
        # Ước lượng 2.5 ký tự/token: prompt + output không được vượt num_ctx.
        self.assertLessEqual(prompt_chars / 2.5 + payload["options"]["num_predict"], CHAT_NUM_CTX)

    def test_repeated_parse_failures_eventually_skip_the_batch(self):
        """JSON hỏng mãi trên cùng lô thì phải bỏ qua lô đó, không kẹt vĩnh viễn."""
        conversation = self._fill(12)
        broken = MemoryExtractionResult(False, [], "JSON không hợp lệ", parse_failed=True)
        statuses = []
        with patch.object(memory_service, "FAILURE_COOLDOWN_SEC", -1), patch(
            "yumeai.services.memory_service.extract_memory_facts", return_value=broken,
        ):
            for _ in range(memory_service.MAX_PARSE_FAILURES):
                statuses.append(refresh_conversation_memory(self.db, conversation["id"])["status"])
        self.assertEqual(statuses[:-1], ["error"] * (len(statuses) - 1))
        self.assertEqual(statuses[-1], "skipped")
        self.assertEqual(self.db.pending_memory_messages(conversation["id"]), [])

    def test_network_errors_never_skip_the_batch(self):
        """Ollama tắt KHÔNG phải lỗi của lô — không được bỏ qua message nào."""
        conversation = self._fill(12)
        down = MemoryExtractionResult(False, [], "Connection refused")
        with patch.object(memory_service, "FAILURE_COOLDOWN_SEC", -1), patch(
            "yumeai.services.memory_service.extract_memory_facts", return_value=down,
        ):
            for _ in range(memory_service.MAX_PARSE_FAILURES + 2):
                refresh_conversation_memory(self.db, conversation["id"])
        self.assertEqual(len(self.db.pending_memory_messages(conversation["id"])), 12)

    def test_invalid_json_is_flagged_as_parse_failure(self):
        self.assertTrue(parse_memory_response("không phải json").parse_failed)
        self.assertTrue(parse_memory_response('{"x": 1}').parse_failed)


if __name__ == "__main__":
    unittest.main()
