"""Test offline cho trí nhớ dài trong chat_context; không cần Ollama.

Cắm một module ollama_service giả vào sys.modules để ĐẾM số lời gọi LLM. Đây là
thứ duy nhất đo được cái giá thật của tóm tắt: trên máy iGPU/CPU mỗi lời gọi là
vài giây tới vài chục giây chặn ngay trước khi người dùng thấy token đầu tiên.
"""

import sys
import types
import unittest


def _install_fake_ollama():
    """Thay ollama_service bằng bản giả ghi lại từng lời gọi."""
    calls = []
    fake = types.ModuleType("yumeai.services.ollama_service")

    def summarize_conversation(messages):
        calls.append(list(messages))
        return f"- đã tóm tắt {len(messages)} message"

    fake.summarize_conversation = summarize_conversation
    sys.modules["yumeai.services.ollama_service"] = fake
    return calls, fake


CALLS, FAKE = _install_fake_ollama()

from yumeai.core.chat_context import clear_memory_cache, summarize_old_history  # noqa: E402
from yumeai.core.config import (  # noqa: E402
    CHAT_HISTORY_MAX_MESSAGES,
    CHAT_SUMMARY_EVERY,
    CHAT_SUMMARY_MAX_MESSAGES,
)


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
        FAKE.summarize_conversation = lambda messages: (
            CALLS.append(list(messages)) or f"- đã tóm tắt {len(messages)} message"
        )

    def test_summary_is_reused_within_one_bucket(self):
        """Trong cùng một lô CHAT_SUMMARY_EVERY chỉ được gọi LLM đúng một lần.

        Bản cũ băm TOÀN BỘ overflow để làm khoá cache. Overflow đổi mỗi lượt nên khoá
        đổi mỗi lượt và cache không bao giờ trúng — mỗi tin nhắn phải trả giá thêm một
        lời gọi LLM.
        """
        base_turns = (CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2
        first = summarize_old_history(_history(base_turns), "Yuki")
        self.assertTrue(first)
        self.assertEqual(len(CALLS), 1)

        # Thêm một lượt nữa nhưng chưa sang lô mới -> phải dùng lại kết quả cũ.
        again = summarize_old_history(_history(base_turns + 1), "Yuki")
        self.assertEqual(again, first)
        self.assertEqual(len(CALLS), 1, "lô chưa đổi mà vẫn gọi lại LLM")

    def test_call_count_grows_slower_than_turn_count(self):
        """60 lượt chat không được thành 60 lời gọi tóm tắt."""
        for turns in range(1, 61):
            summarize_old_history(_history(turns), "Yuki")
        # Mỗi lượt đẩy 2 message ra ngoài cửa sổ, một lô là CHAT_SUMMARY_EVERY message.
        self.assertLessEqual(len(CALLS), 60 * 2 // CHAT_SUMMARY_EVERY + 1)

    def test_payload_sent_to_llm_is_bounded(self):
        """Hội thoại dài mãi thì payload tóm tắt vẫn phải có cận trên."""
        for turns in range(1, 121):
            summarize_old_history(_history(turns), "Yuki")
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
            summarize_old_history(_history(turns), "Yuki")
        first_window = CALLS[0]
        last_window = CALLS[-1]
        self.assertNotEqual(
            [m["content"] for m in first_window],
            [m["content"] for m in last_window],
            "cửa sổ tóm tắt không đổi dù hội thoại đã dài thêm 100 lượt",
        )

    def test_failed_summary_is_not_cached(self):
        """Ollama chết trả rỗng — không được cache, nếu không mất trí nhớ cả phiên."""
        FAKE.summarize_conversation = lambda messages: (CALLS.append(list(messages)) or "")
        history = _history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2)
        self.assertEqual(summarize_old_history(history, "Yuki"), "")
        self.assertEqual(len(CALLS), 1)

        # Dịch vụ sống lại: phải thử lại chứ không trả về "" từ cache.
        FAKE.summarize_conversation = lambda messages: (
            CALLS.append(list(messages)) or "- đã nhớ lại"
        )
        self.assertEqual(summarize_old_history(history, "Yuki"), "- đã nhớ lại")
        self.assertEqual(len(CALLS), 2)

    def test_two_personas_do_not_share_summary(self):
        """Cùng một đoạn hội thoại nhưng hai nhân vật phải tóm tắt riêng."""
        history = _history((CHAT_HISTORY_MAX_MESSAGES + CHAT_SUMMARY_EVERY * 2) // 2)
        summarize_old_history(history, "Yuki")
        summarize_old_history(history, "Hana")
        self.assertEqual(len(CALLS), 2)

    def test_short_history_needs_no_summary(self):
        """Chưa tràn cửa sổ thì không được gọi LLM lần nào."""
        summarize_old_history(_history(CHAT_HISTORY_MAX_MESSAGES // 2), "Yuki")
        self.assertEqual(CALLS, [])


if __name__ == "__main__":
    unittest.main()
