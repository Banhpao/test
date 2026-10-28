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
        # Khoá quen thuộc quy về khoá chuẩn; khoá ngoài FACT_KEYS bị bỏ (D25).
        self.assertEqual(result.facts, [
            {
                "key": "favorite_food_drink",
                "value": "cà phê đen",
                "confidence": 1.0,
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
            self.assertTrue(line.startswith("NGƯỜI DÙNG: câu ") and line.endswith("x" * 90))

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


# Lời người dùng thật trong lần dùng thử 27/10 (TESTLOG) — người dùng giả "Tuấn" nói với Pao.
TUAN_LINES = [
    "Chào Pao, mình là Tuấn, kỹ sư điện tử ở Đà Nẵng.",
    "Hôm nay mình mệt quá, sửa mạch cả ngày mà vẫn lỗi.",
    "Mình thích nhất là cà phê sữa đá, ngày nào cũng uống.",
    "À, mình có nuôi một con chó tên là Bơ.",
    "Pao đang vẽ gì vậy?",
    "Tối nay Tokyo có mưa không? Pao có hay ra ngoài vẽ ký hoạ buổi tối không?",
    "Nói thật đi, Pao là AI đúng không?",
    "Liệt kê giúp mình 5 mẹo để ngủ ngon đi.",
    "What's your favorite painting?",
    "Kể mình nghe chuyện con mèo hoang gần trường đi.",
    "Mai mình phải đi công tác Sài Gòn rồi, chắc vắng vài hôm.",
    "Pao còn nhớ mình tên gì, làm nghề gì, ở đâu không?",
    "Thế mình thích uống gì, với nuôi con gì tên là gì?",
    "Thôi mình đi ngủ đây, chúc Pao ngủ ngon nhé.",
]


def _facts_json(items):
    return json.dumps({"facts": [
        {"key": k, "value": v, "confidence": 0.9, **({"evidence": e} if e else {})}
        for k, v, e in items
    ]}, ensure_ascii=False)


class FactGroundingTests(unittest.TestCase):
    """D25: chỉ giữ fact dựng được từ lời NGƯỜI DÙNG, không phải chuyện của nhân vật."""

    def test_the_seven_wrong_facts_from_27_10_are_dropped(self):
        # Đúng 13 fact Pao đã lưu ngày 27/10 (khoá cũ tự do): 7 sai, 6 đúng về người dùng.
        wrong = [
            ("interest", "vẽ mưa đêm và mèo hoang ở trường", ""),
            ("dislike", "nói về con mèo hoang", ""),
            ("goal", "hoàn thành bài vẽ deadline", ""),
            ("honorific", "Pao", ""),
            ("boundary", "không muốn nhắc đến việc nuôi chó hoặc mèo", ""),
            ("event", "mưa to tại Tokyo tối nay", ""),
            ("relationship_style", "gọi người bằng Pao, dùng từ B-boys", ""),
        ]
        for item in wrong:
            result = parse_memory_response(_facts_json([item]), TUAN_LINES, "Pao")
            self.assertTrue(result.success)
            self.assertEqual(result.facts, [], f"phải bỏ fact sai: {item[0]} = {item[1]}")

    def test_true_facts_about_the_user_are_kept(self):
        result = parse_memory_response(_facts_json([
            ("name", "Tuấn", "mình là Tuấn"),
            ("occupation", "kỹ sư điện tử", "mình là Tuấn, kỹ sư điện tử ở Đà Nẵng"),
            ("home_city", "Đà Nẵng", ""),
            ("pet", "nuôi chó tên Bơ", "À, mình có nuôi một con chó tên là Bơ."),
            ("favorite_food_drink", "cà phê sữa đá", "Mình thích nhất là cà phê sữa đá"),
            ("current_event", "sắp đi công tác Sài Gòn vài hôm", "Mai mình phải đi công tác Sài Gòn rồi"),
        ]), TUAN_LINES, "Pao")
        self.assertEqual(
            [f["key"] for f in result.facts],
            ["name", "occupation", "home_city", "pet", "favorite_food_drink", "current_event"],
        )

    def test_evidence_must_be_a_user_statement(self):
        # Bằng chứng bịa, hoặc chép từ câu hỏi của người dùng, đều không được tính.
        for evidence in ("Tao chỉ uống cà phê đen không đường", "Tối nay Tokyo có mưa không?"):
            result = parse_memory_response(
                _facts_json([("favorite_food_drink", "cà phê sữa đá", evidence)]), TUAN_LINES, "Pao")
            self.assertEqual(result.facts, [], evidence)

    def test_questions_and_requests_are_not_statements(self):
        self.assertTrue(memory_service._is_question("Pao đang vẽ gì vậy?"))
        self.assertTrue(memory_service._is_question("Pao còn nhớ mình tên gì, làm nghề gì, ở đâu không"))
        self.assertTrue(memory_service._is_request("Kể mình nghe chuyện con mèo hoang gần trường đi."))
        self.assertTrue(memory_service._is_request("Liệt kê giúp mình 5 mẹo để ngủ ngon đi."))
        for statement in ("Mai mình phải đi công tác Sài Gòn rồi, chắc vắng vài hôm.",
                          "Làm kỹ sư được 3 năm rồi.", "Đọc sách là sở thích của mình.",
                          "Thôi mình đi ngủ đây, chúc Pao ngủ ngon nhé."):
            self.assertFalse(memory_service._is_question(statement), statement)
            self.assertFalse(memory_service._is_request(statement), statement)

    def test_without_user_lines_parser_only_checks_keys(self):
        # Gọi parser không kèm lời người dùng (API cũ) vẫn chạy như trước, chỉ thêm lọc khoá.
        result = parse_memory_response(_facts_json([("hobby", "đọc sách", ""), ("mood", "vui", "")]))
        self.assertEqual(result.facts, [{"key": "likes", "value": "đọc sách", "confidence": 0.9}])

    def test_request_schema_limits_keys_and_asks_for_evidence(self):
        _FakeClient.payloads = []
        with patch.object(memory_service.httpx, "Client", _FakeClient):
            extract_memory_facts([{"role": "user", "content": "mình tên Tuấn"}], "Pao")
        payload = _FakeClient.payloads[-1]
        item = payload["format"]["properties"]["facts"]["items"]
        self.assertEqual(item["properties"]["key"]["enum"], list(memory_service.FACT_KEYS))
        self.assertIn("evidence", item["required"])
        self.assertIn("Pao", payload["messages"][0]["content"])

    def test_transcript_names_both_speakers(self):
        transcript = build_memory_transcript([
            {"role": "user", "content": "mình thích trà sữa"},
            {"role": "assistant", "content": "Tao uống cà phê đen."},
        ], budget_chars=1000, persona_name="Pao")
        self.assertEqual(transcript.splitlines(), [
            "NGƯỜI DÙNG: mình thích trà sữa",
            "Pao (nhân vật): Tao uống cà phê đen.",
        ])

    def test_memory_context_uses_vietnamese_labels(self):
        context = format_memory_context([
            {"fact_key": "pet", "fact_value": "chó tên Bơ"},
            {"fact_key": "tu_dat", "fact_value": "khoá người dùng tự đặt"},
        ])
        self.assertEqual(context.splitlines(), ["- Thú cưng: chó tên Bơ", "- tu_dat: khoá người dùng tự đặt"])


class SummaryGroundingTests(unittest.TestCase):
    """Chạy lại 28/10: tóm tắt vẫn viết chuyện của Pao dù prompt đã gọi đúng tên hai người."""

    def test_real_summary_from_28_10_is_dropped_entirely(self):
        summary = ("- Pao đang vẽ ký hoạ ngoài trời dưới mưa.\n- Pao không muốn nhắc đến việc mình vẽ ký hoạ.\n"
                   "- Pao là một nghệ sĩ vẽ ký hoạ.\n- Pao sống ở Đà Nẵng.\n"
                   "- Pao gặp khó khăn với deadline công việc.")
        self.assertEqual(memory_service.ground_summary(summary, TUAN_LINES, "Pao"), "")

    def test_lines_about_the_user_are_kept(self):
        summary = ("- Tên: Pao\n- Người dùng tên Tuấn, là kỹ sư điện tử\n- Sống ở Đà Nẵng\n"
                   "- Nuôi chó tên Bơ\n- Hay ra ngoài ký hoạ buổi tối\n- Tối nay Tokyo có mưa")
        self.assertEqual(
            memory_service.ground_summary(summary, TUAN_LINES, "Pao"),
            "- Người dùng tên Tuấn, là kỹ sư điện tử\n- Sống ở Đà Nẵng\n- Nuôi chó tên Bơ",
        )


class EventFactTests(unittest.TestCase):
    """D29: chạy lại 28/10, "Mai mình phải đi công tác Sài Gòn rồi, chắc vắng vài hôm." không
    thành fact — lô đó chỉ có đúng câu này là lời khẳng định của người dùng."""

    def test_prompt_explains_event_and_promise_keys(self):
        prompt = memory_service._extraction_prompt("Pao")
        self.assertIn('"current_event": việc đang hoặc sắp xảy ra', prompt)
        self.assertIn('"promise":', prompt)
        self.assertIn("Giữ nguyên mốc thời gian", prompt)

    def test_trip_fact_passes_grounding(self):
        raw = _facts_json([("current_event", "mai đi công tác Sài Gòn vài hôm",
                            "Mai mình phải đi công tác Sài Gòn rồi, chắc vắng vài hôm.")])
        result = parse_memory_response(raw, TUAN_LINES, "Pao")
        self.assertEqual([(f["key"], f["value"]) for f in result.facts],
                         [("current_event", "mai đi công tác Sài Gòn vài hôm")])

    def test_example_in_prompt_cannot_leak_as_a_fact(self):
        # Model nhỏ hay chép luôn ví dụ trong prompt; người dùng chưa từng nói câu đó.
        raw = _facts_json([("current_event", "tuần sau đi Hà Nội họp, sẽ bận",
                            "Tuần sau mình phải đi Hà Nội họp, chắc bận lắm.")])
        self.assertEqual(parse_memory_response(raw, TUAN_LINES, "Pao").facts, [])

    def test_prompt_asks_for_every_fact_in_a_sentence_and_no_duplicates(self):
        prompt = memory_service._extraction_prompt("Pao")
        self.assertIn("Một câu có thể chứa nhiều điều", prompt)
        self.assertIn('con vật nuôi → "pet"', prompt)

    def test_generic_duplicates_are_dropped_and_do_not_crowd_out(self):
        # Đúng lô đầu của lần chạy lại 28/10 (tối): model ghi con chó và cà phê hai lần.
        raw = _facts_json([
            ("name", "Tuấn", ""), ("occupation", "kỹ sư điện tử", ""),
            ("likes", "cà phê sữa đá", ""), ("family", "có nuôi một con chó tên là Bơ", ""),
            ("favorite_food_drink", "cà phê sữa đá", ""), ("pet", "chó tên Bơ", ""),
            ("home_city", "Đà Nẵng", ""),
        ])
        keys = [f["key"] for f in parse_memory_response(raw, TUAN_LINES, "Pao").facts]
        self.assertEqual(keys, ["name", "occupation", "favorite_food_drink", "pet", "home_city"])

    def test_generic_fact_about_something_else_is_kept(self):
        raw = _facts_json([("favorite_food_drink", "cà phê sữa đá", ""),
                           ("likes", "sửa mạch cả ngày", "")])
        keys = [f["key"] for f in parse_memory_response(raw, TUAN_LINES, "Pao").facts]
        self.assertEqual(keys, ["favorite_food_drink", "likes"])

    def test_examples_in_prompt_cannot_leak_as_facts(self):
        raw = _facts_json([("name", "Lan", ""), ("home_city", "Huế", ""), ("occupation", "giáo viên", "")])
        self.assertEqual(parse_memory_response(raw, TUAN_LINES, "Pao").facts, [])

    def test_events_carry_the_day_they_were_recorded(self):
        context = format_memory_context([
            {"fact_key": "name", "fact_value": "Tuấn", "updated_at": "2026-10-28T01:39:35+00:00"},
            {"fact_key": "current_event", "fact_value": "mai đi công tác Sài Gòn vài hôm",
             "updated_at": "2026-10-28T01:39:35+00:00"},
        ], today="30/10/2026")
        lines = context.splitlines()
        self.assertEqual(lines[0], "- Tên: Tuấn")
        self.assertRegex(lines[1], r"^- Chuyện đang diễn ra \(ghi ngày \d\d/10/2026\): mai đi công tác Sài Gòn vài hôm$")
        self.assertEqual(lines[2], "(Hôm nay là 30/10/2026.)")

    def test_no_dated_fact_means_no_today_line(self):
        self.assertEqual(format_memory_context([{"fact_key": "name", "fact_value": "Tuấn"}]), "- Tên: Tuấn")


class SummaryPromptTests(unittest.TestCase):
    def test_summary_names_the_character_and_drops_empty_lines(self):
        from yumeai.services import ollama_service
        sent = {}

        def fake_chat(model, system, user, **kwargs):
            sent.update(system=system, user=user)
            return "- Tên: Tuấn\n- Tuổi: Chưa có\n- Nghề: kỹ sư điện tử\n- Lời hứa: (không rõ)"

        with patch.object(ollama_service, "_ollama_chat", fake_chat), \
                patch.object(ollama_service, "get_translate_model", return_value="m"):
            out = ollama_service.summarize_conversation([
                {"role": "user", "content": "mình là Tuấn"},
                {"role": "assistant", "content": "Tao là Pao."},
            ], "Pao")
        self.assertIn("Pao (nhân vật): Tao là Pao.", sent["user"])
        self.assertIn("NGƯỜI DÙNG: mình là Tuấn", sent["user"])
        self.assertIn("KHÔNG ghi tên, sở thích, công việc hay chuyện của Pao", sent["system"])
        self.assertEqual(out, "- Tên: Tuấn\n- Nghề: kỹ sư điện tử")


if __name__ == "__main__":
    unittest.main()
