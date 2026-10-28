"""Test offline cho trí nhớ dài: cache tóm tắt + khối trí nhớ dùng chung hai cửa vào.

Cắm module ollama_service giả vào sys.modules để ĐẾM số lời gọi LLM. Đây là thứ duy
nhất đo được cái giá thật của tóm tắt: trên máy iGPU/CPU mỗi lời gọi là vài giây tới
vài chục giây chặn ngay trước khi người dùng thấy token đầu tiên.

`httpx` cũng bị cắm giả để chạy được trên máy chưa cài venv — memory_service import
nó ở mức module nhưng build_conversation_memory không hề dùng tới.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_fake_ollama():
    """Thay ollama_service bằng bản giả ghi lại từng lời gọi."""
    calls = []
    fake = types.ModuleType("yumeai.services.ollama_service")

    def summarize_conversation(messages, persona_name=""):
        calls.append(list(messages))
        return f"- đã tóm tắt {len(messages)} message"

    fake.summarize_conversation = summarize_conversation
    sys.modules["yumeai.services.ollama_service"] = fake
    return calls, fake


CALLS, FAKE = _install_fake_ollama()
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

from yumeai.core.chat_context import (  # noqa: E402
    build_persona_messages,
    clear_memory_cache,
    history_load_limit,
    summarize_old_history,
)
from yumeai.core.config import (  # noqa: E402
    CHAT_HISTORY_MAX_MESSAGES,
    CHAT_SUMMARY_EVERY,
    CHAT_SUMMARY_MAX_MESSAGES,
)
from yumeai.services.memory_service import build_conversation_memory  # noqa: E402
from yumeai.storage import YumeDatabase  # noqa: E402

TEST_PERSONAS = {"pao": {"name": "Pao"}, "su": {"name": "Su"}}


def _history(turns: int) -> list[dict]:
    """Dựng lịch sử `turns` lượt, mỗi lượt một cặp user/assistant."""
    history = []
    for i in range(1, turns + 1):
        history.append({"role": "user", "content": f"câu hỏi số {i}"})
        history.append({"role": "assistant", "content": f"trả lời số {i}"})
    return history


class SummaryCacheTests(unittest.TestCase):
    def setUp(self):
        CALLS.clear()
        clear_memory_cache()
        FAKE.summarize_conversation = lambda messages, persona_name="": (
            CALLS.append(list(messages)) or f"- đã tóm tắt {len(messages)} message"
        )

    def test_summary_is_reused_within_one_bucket(self):
        """Trong cùng một lô CHAT_SUMMARY_EVERY chỉ được gọi LLM đúng một lần.

        Bản cũ băm TOÀN BỘ overflow để làm khoá cache. Overflow đổi mỗi lượt nên khoá
        đổi mỗi lượt và cache không bao giờ trúng — mỗi tin nhắn phải trả giá thêm một
        lời gọi LLM.
        """
        base_turns = (CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2
        first = summarize_old_history(_history(base_turns), "Pao")
        self.assertTrue(first)
        self.assertEqual(len(CALLS), 1)

        # Thêm một lượt nữa nhưng chưa sang lô mới -> phải dùng lại kết quả cũ.
        again = summarize_old_history(_history(base_turns + 1), "Pao")
        self.assertEqual(again, first)
        self.assertEqual(len(CALLS), 1, "lô chưa đổi mà vẫn gọi lại LLM")

    def test_call_count_grows_slower_than_turn_count(self):
        """60 lượt chat không được thành 60 lời gọi tóm tắt."""
        for turns in range(1, 61):
            summarize_old_history(_history(turns), "Pao")
        # Mỗi lượt đẩy 2 message ra ngoài cửa sổ, một lô là CHAT_SUMMARY_EVERY message.
        self.assertLessEqual(len(CALLS), 60 * 2 // CHAT_SUMMARY_EVERY + 1)

    def test_payload_sent_to_llm_is_bounded(self):
        """Hội thoại dài mãi thì payload tóm tắt vẫn phải có cận trên."""
        for turns in range(1, 121):
            summarize_old_history(_history(turns), "Pao")
        self.assertTrue(CALLS)
        self.assertLessEqual(
            max(len(c) for c in CALLS),
            CHAT_SUMMARY_MAX_MESSAGES,
            "payload gửi vào LLM phình theo độ dài hội thoại",
        )

    def test_window_moves_forward_instead_of_freezing_on_oldest(self):
        """Cửa sổ tóm tắt phải trôi theo hội thoại, không đóng băng ở đoạn đầu.

        Bản cũ cắt transcript bằng [:6000] — giữ 6000 ký tự ĐẦU — nên khi hội thoại dài
        ra, phần mới nhất không bao giờ vào được tóm tắt.
        """
        for turns in range(1, 121):
            summarize_old_history(_history(turns), "Pao")
        first_window = CALLS[0]
        last_window = CALLS[-1]
        self.assertNotEqual(
            [m["content"] for m in first_window],
            [m["content"] for m in last_window],
            "cửa sổ tóm tắt không đổi dù hội thoại đã dài thêm 100 lượt",
        )

    def test_failed_summary_is_not_cached(self):
        """Ollama chết trả rỗng — không được cache, nếu không mất trí nhớ cả phiên."""
        FAKE.summarize_conversation = lambda messages, persona_name="": (CALLS.append(list(messages)) or "")
        history = _history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2)
        self.assertEqual(summarize_old_history(history, "Pao"), "")
        self.assertEqual(len(CALLS), 1)

        # Dịch vụ sống lại: phải thử lại chứ không trả về "" từ cache.
        FAKE.summarize_conversation = lambda messages, persona_name="": (
            CALLS.append(list(messages)) or "- đã nhớ lại"
        )
        self.assertEqual(summarize_old_history(history, "Pao"), "- đã nhớ lại")
        self.assertEqual(len(CALLS), 2)

    def test_summary_is_told_who_the_character_is(self):
        """Không có tên nhân vật, model ghi "Tên: Pao" như thể đó là tên người dùng (27/10, D25)."""
        names = []
        FAKE.summarize_conversation = lambda messages, persona_name="": (
            names.append(persona_name) or "- đã tóm tắt"
        )
        summarize_old_history(_history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2), "Su")
        self.assertEqual(names, ["Su"])

    def test_two_personas_do_not_share_summary(self):
        """Cùng một đoạn hội thoại nhưng hai nhân vật phải tóm tắt riêng."""
        history = _history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2)
        summarize_old_history(history, "Pao")
        summarize_old_history(history, "Su")
        self.assertEqual(len(CALLS), 2)

    def test_short_history_needs_no_summary(self):
        """Chưa tràn cửa sổ thì không được gọi LLM lần nào."""
        summarize_old_history(_history(CHAT_HISTORY_MAX_MESSAGES // 2), "Pao")
        self.assertEqual(CALLS, [])


class HistoryLoadLimitTests(unittest.TestCase):
    """Chỉ đọc phần cuối hội thoại từ DB mà ngữ cảnh gửi LLM phải GIỐNG HỆT khi đọc hết.

    Lệch ranh giới lô là khoá cache tóm tắt đổi mỗi lượt — đúng lỗi D13.
    """

    def setUp(self):
        CALLS.clear()
        clear_memory_cache()
        FAKE.summarize_conversation = lambda messages, persona_name="": (
            CALLS.append(list(messages)) or f"- tóm tắt {len(messages)} message"
        )

    @staticmethod
    def _messages(total):
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(total)
        ]

    @staticmethod
    def _tail(messages):
        limit = history_load_limit(len(messages))
        return messages if limit is None else messages[len(messages) - limit:]

    def _summary_window(self, history):
        CALLS.clear()
        clear_memory_cache()
        summarize_old_history(history, "Pao")
        return CALLS[-1] if CALLS else None

    def test_loaded_tail_gives_identical_context(self):
        for total in range(240):
            full = self._messages(total)
            tail = self._tail(full)
            self.assertEqual(
                self._summary_window(tail), self._summary_window(full),
                f"cửa sổ tóm tắt lệch khi hội thoại có {total} message",
            )
            self.assertEqual(
                build_persona_messages({"name": "Pao"}, tail, "câu mới",
                                       memory_summary="", use_fewshot=False),
                build_persona_messages({"name": "Pao"}, full, "câu mới",
                                       memory_summary="", use_fewshot=False),
            )

    def test_loaded_tail_is_bounded(self):
        bound = CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_MAX_MESSAGES + 2 * CHAT_SUMMARY_EVERY
        for total in (100, 1000, 100000):
            self.assertLessEqual(history_load_limit(total), bound)

    def test_cache_still_hits_with_loaded_tail(self):
        """Đọc phần đuôi mà mỗi lượt lại gọi LLM tóm tắt thì tệ hơn cả đọc hết."""
        for total in range(1, 241):
            summarize_old_history(self._tail(self._messages(total)), "Pao")
        tail_calls = len(CALLS)
        CALLS.clear()
        clear_memory_cache()
        for total in range(1, 241):
            summarize_old_history(self._messages(total), "Pao")
        self.assertEqual(tail_calls, len(CALLS))


class SharedMemoryContextTests(unittest.TestCase):
    """build_conversation_memory là hàm DÙNG CHUNG cho main.py và app_gradio.py.

    Nó nằm ở một chỗ chính vì D6: trước đây hai entrypoint tự dựng ngữ cảnh theo hai
    cách rồi cùng một câu cho ra hai kết quả khác nhau.
    """

    def setUp(self):
        CALLS.clear()
        clear_memory_cache()
        FAKE.summarize_conversation = lambda messages, persona_name="": (
            CALLS.append(list(messages)) or f"- tóm tắt {len(messages)} message"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.db = YumeDatabase(Path(self.tmp.name) / "yumeai.db")
        self.db.initialize(TEST_PERSONAS)
        self.conv = self.db.get_or_create_conversation("user_001", "pao")

    def tearDown(self):
        self.tmp.cleanup()

    def _long_history(self):
        return _history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2)

    def test_summary_is_written_back_to_the_database(self):
        """Tóm tắt phải sống qua restart, không tính lại mỗi lần mở app."""
        memory = build_conversation_memory(
            self.db, self.conv, self._long_history(), "Pao"
        )
        self.assertTrue(memory)
        stored = self.db.get_conversation(self.conv["id"])["memory_summary"]
        self.assertEqual(stored, memory)

    def test_stored_summary_is_not_concatenated_with_the_fresh_one(self):
        """Không được ghép bản đã lưu với bản vừa tính — nhét trí nhớ hai lần."""
        history = self._long_history()
        first = build_conversation_memory(self.db, self.conv, history, "Pao")
        reloaded = self.db.get_conversation(self.conv["id"])
        second = build_conversation_memory(self.db, reloaded, history, "Pao")
        self.assertEqual(second, first)

    def test_stored_summary_is_the_fallback_when_ollama_is_down(self):
        """Ollama chết thì dùng bản đã lưu, không để nhân vật mất sạch trí nhớ."""
        history = self._long_history()
        good = build_conversation_memory(self.db, self.conv, history, "Pao")
        reloaded = self.db.get_conversation(self.conv["id"])

        clear_memory_cache()
        FAKE.summarize_conversation = lambda messages, persona_name="": ""
        degraded = build_conversation_memory(self.db, reloaded, history, "Pao")
        self.assertEqual(degraded, good)

    def test_facts_are_included_in_the_memory_block(self):
        self.db.upsert_memory_fact("user_001", "pao", "ten_goi", "Bánh Páo")
        memory = build_conversation_memory(self.db, self.conv, _history(2), "Pao")
        self.assertIn("ten_goi: Bánh Páo", memory)

    def test_two_characters_keep_separate_facts(self):
        """Fact theo cặp user–nhân vật: Su không đọc được trí nhớ của Pao."""
        self.db.upsert_memory_fact("user_001", "pao", "bi_mat", "chỉ kể cho Pao")
        su_conv = self.db.get_or_create_conversation("user_001", "su")
        memory = build_conversation_memory(self.db, su_conv, _history(2), "Su")
        self.assertNotIn("bi_mat", memory)


if __name__ == "__main__":
    unittest.main()
