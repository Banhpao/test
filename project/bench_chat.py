"""
bench_chat — SO SÁNH MODEL CHAT trên máy thật bằng cùng một bộ hội thoại nhập vai (STATE việc 5).

    python bench_chat.py --check-only                         # Ollama/model/VRAM, chưa chat
    python bench_chat.py                                      # model đang cấu hình (OLLAMA_MODEL)
    python bench_chat.py --models qwen2.5:3b hermes3:3b       # nhiều model, lần lượt từng cái
    python bench_chat.py --models qwen2.5:3b --rounds 3       # nhiều vòng hơn → số ổn định hơn

Đo gì, cho từng model:
  1. Nạp model: thời gian nạp cold (sau khi nhả mọi model đang nạp), model chiếm bao nhiêu và
     bao nhiêu phần nằm trên GPU (Ollama /api/ps), RAM trống trước/sau khi nạp.
  2. Mỗi câu thoại: token/giây, chờ chữ đầu, thời gian cả câu, số token prompt — từ chunk cuối
     của Ollama, giống hệt số /api/stats của app ghi (yumeai/core/perf.py).
  3. Giữ vai, chấm bằng LUẬT chứ không nhờ model khác chấm: trả lời bằng tiếng Việt, không lẫn
     chữ Hán, không emoji, không gạch đầu dòng, không tự nhận là AI, xưng hô đúng nhân vật
     (Pao: tao/mày ↔ tớ/cậu; Su: em/anh), không dài dòng hay bị cắt giữa câu, nhớ đúng thông tin
     trong trí nhớ. Mỗi câu trả lời được in ra và lưu lại để đọc bằng mắt — luật chỉ bắt được
     lỗi thô, không thay được người đọc.

Đi ĐÚNG đường của app: prompt dựng bằng build_persona_messages() + chat_options() thật (system
prompt, few-shot, cửa sổ lịch sử, num_ctx, num_gpu), hội thoại bắt đầu bằng lời chào của nhân vật
như một cuộc trò chuyện mới trong /app, câu trả lời của model được đưa lại làm lịch sử cho câu
sau. Trí nhớ là ba fact giả cố định (format_memory_context thật), không đọc DB.

Khác app ở hai chỗ, có chủ đích: mỗi vòng cố định options.seed để chạy lại ra gần như cùng
câu; và không bao giờ gọi bộ tóm tắt hay trích fact (memory_summary đã có sẵn).

Không làm:
  - Không tải model. Model chưa pull thì bỏ qua và in lệnh `ollama pull` để bạn tự chạy.
  - Không đụng DB, gallery, ComfyUI. Không đổi biến môi trường.
  - Có nhả model Ollama khỏi bộ nhớ (keep_alive=0) trước và sau mỗi model để model sau bắt đầu
    từ bộ nhớ trống — app sẽ tự nạp lại ở câu chat kế tiếp. Không muốn: --no-free.
  - Từ chối chạy khi ComfyUI đang vẽ (GPU bận → số đo sai).

Kết quả: <YUMEAI_DATA_DIR, mặc định project/data>/bench/chat_<thời điểm>.json (kèm toàn bộ câu
trả lời) và ..._testlog.md chứa khối dán vào docs/TESTLOG.md (khối này cũng được in ra).

Mã thoát: 0 = đo xong; 1 = có câu lỗi (phần đo được vẫn được ghi); 2 = không bắt đầu được
(Ollama tắt, không có model nào đã pull, ComfyUI đang vẽ); 130 = dừng bằng Ctrl+C.

Hàm dò máy (nvidia-smi, RAM, ComfyUI, model Ollama đang nạp) lấy từ bench_baseline.py — đừng
chép sang đây (xem docstring của file đó).
"""
import argparse
import json
import os
import platform
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

import bench_baseline as bb
from yumeai.core.chat_context import build_persona_messages, chat_options
from yumeai.core.config import (
    CHAT_FEWSHOT,
    CHAT_HISTORY_MAX_MESSAGES,
    CHAT_NUM_CTX,
    CHAT_NUM_PREDICT,
    CHAT_TEMPERATURE,
    CHAT_TIMEOUT_SEC,
    DATA_DIR,
    OLLAMA_MODEL,
    OLLAMA_NUM_GPU,
    OLLAMA_URL,
)
from yumeai.core.perf import ollama_chunk_stats
from yumeai.core.personas import PERSONAS
from yumeai.services.memory_service import format_memory_context

BENCH_SCHEMA = "yumeai.bench.chat/1"
DEFAULT_ROUNDS = 2
MAX_ROUNDS = 10
DEFAULT_SEED = 20261022
MAX_SEED = 2**31 - 1
HTTP_TIMEOUT_S = 5.0
LOAD_TIMEOUT_S = 600.0     # model 3-4B nạp từ HDD/RAM chậm có thể mất vài phút
SETTLE_S = 1.0             # chờ Ollama nhả bộ nhớ xong rồi mới đo RAM/nạp model sau

# ══════════════════════════════════════════════════════════════════════════
# BỘ HỘI THOẠI CỐ ĐỊNH — đổi thì số cũ không còn so được với số mới
# ══════════════════════════════════════════════════════════════════════════
USER_NAME = "Minh"
BENCH_FACTS = [
    {"fact_key": "ten_goi", "fact_value": "Minh"},
    {"fact_key": "cong_viec", "fact_value": "lập trình viên, hay làm tới khuya"},
    {"fact_key": "do_uong_yeu_thich", "fact_value": "trà sữa ít đường"},
]
MEMORY_BLOCK = format_memory_context(BENCH_FACTS)

# probe: câu này cố ý dụ model phạm luật nào (chỉ để đọc báo cáo; luật nào cũng chấm mọi câu).
# expect_any: câu trả lời phải chứa ít nhất một chuỗi (không phân biệt hoa thường).
SCRIPTS = {
    "pao": [
        {"id": "chao", "text": "Chào em, hôm nay em thế nào?"},
        {"id": "nho_ten", "text": "Em còn nhớ anh tên gì không đấy?", "expect_any": ["Minh"],
         "probe": "trí nhớ"},
        {"id": "liet_ke", "text": "Kể cho anh ba món em thích ăn nhất đi", "probe": "gạch đầu dòng"},
        {"id": "hoi_ai", "text": "Nói thật đi, em là AI đúng không?", "probe": "nhận là AI"},
        {"id": "tieng_anh", "text": "What are you drawing right now?", "probe": "trả lời tiếng Anh"},
        {"id": "tam_su", "text": "Anh mệt quá, hôm nay bị sếp mắng cả buổi", "probe": "dài dòng, giảng giải"},
    ],
    "su": [
        {"id": "chao", "text": "Chào em, tối nay em đang làm gì thế?"},
        {"id": "nho_do_uong", "text": "Em còn nhớ anh hay uống gì không?", "expect_any": ["trà sữa"],
         "probe": "trí nhớ"},
        {"id": "liet_ke", "text": "Liệt kê giúp anh mấy cuốn sách em thích nhé", "probe": "gạch đầu dòng"},
        {"id": "hoi_ai", "text": "Em là chatbot phải không?", "probe": "nhận là AI"},
        {"id": "tieng_anh", "text": "Tell me about the novel you are writing.", "probe": "trả lời tiếng Anh"},
        {"id": "tam_su", "text": "Anh mệt quá, hôm nay bị sếp mắng cả buổi", "probe": "dài dòng, giảng giải"},
    ],
}

# Xưng hô theo speech_style trong personas.py. "right": có ít nhất một từ này là đúng giọng;
# "wrong": có từ này là tuột vai ("tôi" là dấu hiệu rõ nhất model quay về giọng trợ lý).
PRONOUNS = {
    "pao": {"right": {"tao", "mày", "tớ", "cậu"}, "wrong": {"em", "tôi"}},
    "su": {"right": {"em", "anh"}, "wrong": {"tao", "mày", "tớ", "cậu", "tôi"}},
}
# Pao nói 1-3 câu, Su 2-3 câu (speech_style); trần số từ để lại chỗ cho "..." và tiếng Nhật chêm.
MAX_WORDS = {"pao": 70, "su": 110}
DEFAULT_MAX_WORDS = 120

# Biến môi trường đổi được kết quả phép đo — ghi lại để hai lần đo so được với nhau.
CHAT_ENV = (
    "OLLAMA_URL", "OLLAMA_MODEL", "YUMEAI_DATA_DIR", "YUMEAI_OLLAMA_NUM_GPU", "YUMEAI_CHAT_NUM_CTX",
    "YUMEAI_CHAT_NUM_PREDICT", "YUMEAI_CHAT_TEMPERATURE", "YUMEAI_CHAT_TOP_P",
    "YUMEAI_CHAT_REPEAT_PENALTY", "YUMEAI_CHAT_FEWSHOT", "YUMEAI_CHAT_HISTORY_MAX",
)

CHECKS = (
    ("tieng_viet", "Tiếng Việt"),
    ("khong_chu_han", "Không chữ Hán"),
    ("khong_emoji", "Không emoji"),
    ("khong_liet_ke", "Không liệt kê"),
    ("khong_nhan_ai", "Không nhận AI"),
    ("xung_ho", "Xưng hô"),
    ("do_dai", "Độ dài"),
    ("nho_dung", "Nhớ đúng"),
)

# ══════════════════════════════════════════════════════════════════════════
# CHẤM GIỮ VAI BẰNG LUẬT
# ══════════════════════════════════════════════════════════════════════════
_VIET_LETTERS = set("àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ")
_WORD_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
# Hán tự, kana, hangul. Model Qwen nhỏ hay lẫn tiếng Trung giữa câu tiếng Việt.
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍]"
)
_LIST_RE = re.compile(r"^\s*(?:[-*•+]|\d{1,2}[.)])\s+\S", re.MULTILINE)
# Từ tiếng Anh không trùng chữ tiếng Việt không dấu ("to", "do", "can", "me" là tiếng Việt).
_ENGLISH_WORDS = {
    "the", "you", "your", "is", "are", "am", "what", "and", "of", "it", "my", "this", "that",
    "with", "for", "have", "was", "were", "i", "i'm", "don't", "it's", "just", "about", "would",
}
_SELF = r"(?:tôi|tao|tớ|em|mình)"
# "AI" chỉ khớp chữ HOA: "ai" thường là đại từ "ai" (who) — "đừng hỏi tao là ai" không phải nhận là AI.
_AI_TERMS = r"(?:một\s+)?(?:(?-i:AI)|A\.I\.|trí tuệ nhân tạo|chat\s?bot|trợ lý(?: ảo| (?-i:AI))?|mô hình ngôn ngữ|máy tính|robot|chương trình)"
_AI_ADMIT_RE = re.compile(rf"(?<!\w){_SELF}\s+(?:chỉ\s+|đúng\s+|thật\s+)?là\s+{_AI_TERMS}(?!\w)", re.IGNORECASE)
# Ngay sau là câu hỏi ngược / phủ định kiểu "tao là AI à?", "tao là máy đâu mà" → vẫn trong vai.
_DEFLECT_RE = re.compile(r"^\s*(?:[^.!\n]{0,12}\?|à|hả|ư|sao|hử|chắc|đâu|á\b|cái gì|gì)", re.IGNORECASE)
# Cụm không bao giờ xuất hiện khi còn trong vai.
_BREAK_ROLE_RE = re.compile(
    r"as an AI|I am an AI|I'm an AI|language model|mô hình ngôn ngữ|trợ lý ảo|"
    r"được (?:huấn luyện|đào tạo|phát triển) bởi|OpenAI|Anthropic|Alibaba|\bQwen\b|\bLlama\b|"
    r"Nous Research|Hermes",
    re.IGNORECASE,
)


def words_of(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def is_vietnamese(text: str) -> bool:
    """Đủ chữ có dấu tiếng Việt và không có câu tiếng Anh. Câu quá ngắn ("Hừ.") coi là đạt."""
    words = words_of(text)
    if not words:
        return False
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    if english >= 3:
        return False
    if len(words) < 4:
        return True
    accented = sum(1 for w in words if any(ch in _VIET_LETTERS for ch in w))
    return accented / len(words) >= 0.2


def admits_ai(text: str) -> bool:
    if _BREAK_ROLE_RE.search(text or ""):
        return True
    for match in _AI_ADMIT_RE.finditer(text or ""):
        if not _DEFLECT_RE.match(text[match.end():]):
            return True
    return False


def pronoun_check(persona_id: str, text: str):
    """True = xưng hô đúng giọng; False = có từ tuột vai; None = câu không có đại từ nào."""
    rule = PRONOUNS.get(persona_id)
    if not rule:
        return None
    found = set(words_of(text))
    if found & rule["wrong"]:
        return False
    if found & rule["right"]:
        return True
    return None


def check_reply(persona_id: str, turn: dict, reply: str, done_reason: str | None = None) -> dict:
    """Chấm một câu trả lời. Mỗi luật: True đạt, False trượt, None không áp dụng."""
    words = words_of(reply)
    limit = MAX_WORDS.get(persona_id, DEFAULT_MAX_WORDS)
    expect = turn.get("expect_any") or []
    lowered = (reply or "").lower()
    checks = {
        "tieng_viet": is_vietnamese(reply),
        "khong_chu_han": not _CJK_RE.search(reply or ""),
        "khong_emoji": not _EMOJI_RE.search(reply or ""),
        "khong_liet_ke": not _LIST_RE.search(reply or "") and "**" not in (reply or ""),
        "khong_nhan_ai": not admits_ai(reply or ""),
        "xung_ho": pronoun_check(persona_id, reply or ""),
        "do_dai": bool(words) and len(words) <= limit and done_reason != "length",
        "nho_dung": (any(e.lower() in lowered for e in expect)) if expect else None,
    }
    failed = [key for key, value in checks.items() if value is False]
    return {"checks": checks, "failed": failed, "ok": not failed, "words": len(words)}


# ══════════════════════════════════════════════════════════════════════════
# OLLAMA
# ══════════════════════════════════════════════════════════════════════════
class BenchError(RuntimeError):
    pass


def installed_models(url: str = OLLAMA_URL, timeout: float = HTTP_TIMEOUT_S):
    """Tên các model đã pull (GET /api/tags), hoặc None nếu Ollama không trả lời."""
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{url}/api/tags")
            r.raise_for_status()
            items = r.json().get("models") or []
    except Exception:
        return None
    names = []
    for item in items:
        name = item.get("name") or item.get("model")
        if name and name not in names:
            names.append(name)
    return names


def resolve_model(requested: str, installed: list[str]):
    """'hermes3' khớp 'hermes3:latest' như cách Ollama tự hiểu; không đoán tag khác."""
    if requested in installed:
        return requested
    if ":" not in requested and f"{requested}:latest" in installed:
        return f"{requested}:latest"
    return None


def unload_models(names, url: str = OLLAMA_URL, timeout: float = 30.0) -> list[str]:
    """keep_alive=0 cho từng model → Ollama nhả khỏi VRAM/RAM. Best-effort."""
    done = []
    with httpx.Client(timeout=timeout) as c:
        for name in names:
            try:
                r = c.post(f"{url}/api/generate", json={"model": name, "keep_alive": 0})
                if r.status_code < 400:
                    done.append(name)
            except Exception:
                pass
    return done


def bench_options(seed: int) -> dict:
    # Đúng options của app (gồm num_ctx/num_gpu — khác nhau là Ollama nạp lại model) + seed.
    return {**chat_options(), "seed": seed}


def warm_up(model: str, url: str = OLLAMA_URL, timeout: float = LOAD_TIMEOUT_S) -> dict:
    """Nạp model bằng MỘT lời gọi chat cùng options với lúc đo, để đo thời gian nạp cold.

    Dùng /api/chat chứ không /api/generate: num_ctx/num_gpu phải giống hệt các câu sau,
    nếu không Ollama nạp lại model ở câu đầu và số "chờ chữ đầu" của câu đó bị đội lên.
    """
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(f"{url}/api/chat", json={
                "model": model,
                "messages": [{"role": "user", "content": "chào"}],
                "stream": False,
                "think": False,
                "options": {**chat_options(), "num_predict": 1},
            })
            if r.status_code >= 400:
                raise BenchError(_ollama_error(r))
            data = r.json()
    except Exception as e:
        return {"ok": False, "wall_s": round(time.perf_counter() - started, 2), "error": bb._err(e)}
    stats = ollama_chunk_stats(data)
    return {"ok": True, "wall_s": round(time.perf_counter() - started, 2), "load_s": stats.get("load_s")}


def _ollama_error(response) -> str:
    try:
        detail = response.json().get("error")
    except Exception:
        detail = None
    return f"HTTP {response.status_code} từ Ollama" + (f": {detail}" if detail else "")


def stream_chat(model: str, messages: list[dict], options: dict, *, url: str = OLLAMA_URL,
                timeout: float = CHAT_TIMEOUT_SEC) -> dict:
    """Một lượt chat streaming y như /api/chat của main.py; đo chờ chữ đầu bằng đồng hồ thật."""
    started = time.perf_counter()
    first = None
    parts = []
    final = None
    with httpx.Client(timeout=timeout) as c:
        with c.stream("POST", f"{url}/api/chat", json={
            "model": model, "messages": messages, "stream": True, "think": False, "options": options,
        }) as r:
            if r.status_code >= 400:
                r.read()
                raise BenchError(_ollama_error(r))
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("error"):
                    raise BenchError(f"Ollama báo lỗi giữa chừng: {d['error']}")
                tok = (d.get("message") or {}).get("content") or ""
                if tok:
                    if first is None:
                        first = time.perf_counter()
                    parts.append(tok)
                if d.get("done"):
                    final = d
                    break
    if final is None:
        raise BenchError("Ollama đóng kết nối trước khi gửi chunk cuối (done)")
    return {
        "reply": "".join(parts).strip(),
        "first_token_s": None if first is None else round(first - started, 3),
        "total_s": round(time.perf_counter() - started, 3),
        **ollama_chunk_stats(final),
    }


# ══════════════════════════════════════════════════════════════════════════
# CHẠY MỘT MODEL
# ══════════════════════════════════════════════════════════════════════════
def _loaded_entry(model: str):
    for item in bb.ollama_loaded_models() or []:
        if item["name"] == model:
            return item
    return None


def run_model(model: str, *, personas: list[str], rounds: int, seed: int, free: bool,
              timeout: float, sink: list | None = None, out=print) -> dict:
    """Đo một model. rec được nối vào sink NGAY từ đầu để Ctrl+C giữa chừng vẫn giữ phần đã đo."""
    rec = {"model": model, "load": None, "memory": None, "turns": [], "errors": 0,
           "unloaded_before": [], "unloaded_after": False}
    if sink is not None:
        sink.append(rec)
    bb._section(f"Model {model}")
    if free:
        loaded = [m["name"] for m in bb.ollama_loaded_models() or []]
        if loaded:
            rec["unloaded_before"] = unload_models(loaded)
            freed = ', '.join(rec['unloaded_before']) or '(Ollama không nhận lệnh nhả)'
            out(f"  🧹 Nhả {freed} khỏi bộ nhớ để đo {model} nạp từ đầu (cold)")
            time.sleep(SETTLE_S)
    ram_before = bb.read_ram().get("available_mib")
    out("  ⏳ Nạp model...")
    rec["load"] = warm_up(model)
    if not rec["load"]["ok"]:
        rec["errors"] += 1
        out(f"  ❌ Không nạp được {model}: {rec['load']['error']}")
        return rec
    entry = _loaded_entry(model)
    ram_after = bb.read_ram().get("available_mib")
    rec["memory"] = {
        "size_mib": entry["size_mib"] if entry else None,
        "vram_mib": entry["vram_mib"] if entry else None,
        "gpu_pct": (round(100 * entry["vram_mib"] / entry["size_mib"]) if entry and entry["size_mib"] else None),
        "ram_available_before_mib": ram_before,
        "ram_available_after_mib": ram_after,
    }
    out(f"  ✅ Nạp xong sau {_load_text(rec['load'])} — {_memory_text(rec['memory'])}")

    for round_index in range(rounds):
        round_seed = seed + round_index
        for persona_id in personas:
            persona = PERSONAS[persona_id]
            history = [{"role": "assistant", "content": persona.get("greeting", "")}]
            out(f"\n  — Vòng {round_index + 1}/{rounds} · {persona['name']} · seed {round_seed}")
            for turn in SCRIPTS[persona_id]:
                messages = build_persona_messages(
                    persona, history, turn["text"], memory_summary=MEMORY_BLOCK, user_name=USER_NAME,
                )
                row = {"round": round_index + 1, "persona": persona_id, "turn": turn["id"],
                       "user": turn["text"], "seed": round_seed}
                try:
                    res = stream_chat(model, messages, bench_options(round_seed), timeout=timeout)
                except Exception as e:
                    rec["errors"] += 1
                    row["error"] = bb._err(e)
                    rec["turns"].append(row)
                    out(f"    ❌ {turn['text']} → lỗi: {row['error']}")
                    break   # lịch sử đã hỏng; sang nhân vật kế tiếp
                row.update(res)
                row.update(check_reply(persona_id, turn, res["reply"], res.get("done_reason")))
                rec["turns"].append(row)
                history += [{"role": "user", "content": turn["text"]},
                            {"role": "assistant", "content": res["reply"]}]
                out(_turn_text(row))

    if free:
        rec["unloaded_after"] = bool(unload_models([model]))
    rec["summary"] = summarize_model(rec)
    return rec


def _load_text(load: dict) -> str:
    """Thời gian nạp cold: số Ollama tự đo (load_duration) nếu có, không thì đồng hồ ngoài."""
    if isinstance(load.get("load_s"), (int, float)):
        return f"{load['load_s']:.1f}s"
    return f"{load['wall_s']:.1f}s (đồng hồ ngoài)"


def _memory_text(memory) -> str:
    if not memory or memory["size_mib"] is None:
        return "không đọc được dung lượng (Ollama /api/ps)"
    text = f"chiếm {memory['size_mib'] / 1024:.1f} GiB, {memory['gpu_pct']}% trên GPU"
    before, after = memory["ram_available_before_mib"], memory["ram_available_after_mib"]
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        text += f", RAM trống {before / 1024:.1f} → {after / 1024:.1f} GiB"
    return text


def _turn_text(row: dict) -> str:
    mark = "✅" if row["ok"] else "⚠️ " + ", ".join(dict(CHECKS)[k] for k in row["failed"])
    speed = row.get("tokens_per_s")
    speed_text = f"{speed:.1f} tok/s" if speed else "— tok/s"
    first = row.get("first_token_s")
    first_text = f"chữ đầu {first:.1f}s" if first is not None else "không có chữ"
    reply = " ".join((row.get("reply") or "(trống)").split())
    return f"    🧑 {row['user']}\n    💬 {reply}\n       {mark} · {speed_text} · {first_text} · {row['total_s']:.1f}s"


def summarize_model(rec: dict) -> dict:
    done = [t for t in rec["turns"] if "error" not in t]
    per_check = {}
    for key, _label in CHECKS:
        applicable = [t["checks"][key] for t in done if t["checks"][key] is not None]
        per_check[key] = {"passed": sum(1 for v in applicable if v), "applicable": len(applicable)}
    return {
        "turns": len(rec["turns"]),
        "answered": len(done),
        "errors": rec["errors"],
        "role_ok": sum(1 for t in done if t["ok"]),
        "checks": per_check,
        "truncated": sum(1 for t in done if t.get("done_reason") == "length"),
        "tokens_per_s": bb.time_stats(t.get("tokens_per_s") for t in done),
        "first_token_s": bb.time_stats(t.get("first_token_s") for t in done),
        "total_s": bb.time_stats(t.get("total_s") for t in done),
        "prompt_tokens": bb.time_stats(t.get("prompt_tokens") for t in done),
        "output_tokens": bb.time_stats(t.get("output_tokens") for t in done),
    }


# ══════════════════════════════════════════════════════════════════════════
# KẾT QUẢ
# ══════════════════════════════════════════════════════════════════════════
def bench_config() -> dict:
    return {
        "OLLAMA_URL": OLLAMA_URL,
        "OLLAMA_MODEL": OLLAMA_MODEL,
        "num_ctx": CHAT_NUM_CTX,
        "num_predict": CHAT_NUM_PREDICT,
        "temperature": CHAT_TEMPERATURE,
        "num_gpu": OLLAMA_NUM_GPU,
        "fewshot": CHAT_FEWSHOT,
        "history_max_messages": CHAT_HISTORY_MAX_MESSAGES,
        "env": {k: os.environ[k] for k in CHAT_ENV if k in os.environ},
    }


def _num(stats, key="median", fmt="{:.1f}", suffix=""):
    if not stats:
        return "—"
    return fmt.format(stats[key]) + suffix


def _ratio(part) -> str:
    return f"{part['passed']}/{part['applicable']}" if part["applicable"] else "—"


def format_testlog_block(result: dict) -> str:
    env = result["environment"]
    gpu = ((env.get("gpu") or {}).get("gpus") or [None])[0]
    ram = env.get("ram") or {}
    cfg = result["config"]
    comfy = result["services"].get("comfyui") or {}
    models = result["models"]
    lines = []
    machine = f"{gpu['name']} {gpu['total_mib']} MiB" if gpu else "GPU không đọc được"
    lines.append(f"## {result['started_at'][:10]} — {machine}, RAM {bb._fmt_gib(ram.get('total_mib'))}"
                 " — so sánh model chat (bench_chat)")
    lines.append("")
    lines.append("**Phần cứng**")
    lines.append(f"- GPU: {gpu['name'] if gpu else (env.get('gpu') or {}).get('note', '—')}")
    lines.append(f"- RAM: {bb._fmt_gib(ram.get('total_mib'))}")
    lines.append(f"- HĐH: {env.get('platform')}; Python {env.get('python')}")
    lines.append("")
    lines.append("**Cấu hình chạy**")
    lines.append(f"- Model: {', '.join(m['model'] for m in models) or '—'}"
                 + (f"; chưa pull: {', '.join(result['missing'])}" if result["missing"] else ""))
    num_gpu = "Ollama tự quyết" if cfg["num_gpu"] is None else cfg["num_gpu"]
    lines.append(f"- num_ctx {cfg['num_ctx']}, num_predict {cfg['num_predict']}, temperature "
                 f"{cfg['temperature']}, num_gpu {num_gpu}, few-shot {'bật' if cfg['fewshot'] else 'tắt'}, "
                 f"cửa sổ {cfg['history_max_messages']} tin")
    turns = sum(len(SCRIPTS[p]) for p in result["args"]["personas"])
    lines.append(f"- Bộ thoại cố định: {', '.join(PERSONAS[p]['name'] for p in result['args']['personas'])} — "
                 f"{turns} câu × {result['args']['rounds']} vòng, seed {result['args']['seed']}+vòng; "
                 f"trí nhớ giả {len(BENCH_FACTS)} fact; nhả model trước/sau: "
                 f"{'không' if result['args']['no_free'] else 'có'}")
    if comfy.get("up"):
        dev = (comfy.get("devices") or [{}])[0]
        lines.append(f"- ComfyUI đang chạy (không vẽ) — VRAM trống theo ComfyUI: "
                     f"{bb._fmt_mib(dev.get('vram_free_mib'), dev.get('vram_total_mib'))}")
    else:
        lines.append("- ComfyUI: tắt")
    lines.append("")
    lines.append("**Kết quả**")
    lines.append("| Model | Nạp cold | Bộ nhớ | Token/giây | Chờ chữ đầu | Cả câu | Giữ vai | Ghi chú |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for m in models:
        s = m.get("summary")
        if not m["load"] or not m["load"].get("ok"):
            lines.append(f"| {bb._cell(m['model'])} | lỗi | — | — | — | — | — | "
                         f"{bb._cell(m['load']['error'] if m['load'] else 'không chạy')} |")
            continue
        mem = m.get("memory") or {}
        mem_text = (f"{mem['size_mib'] / 1024:.1f} GiB, {mem['gpu_pct']}% GPU"
                    if mem.get("size_mib") else "—")
        notes = []
        if s["errors"]:
            notes.append(f"{s['errors']} câu lỗi")
        if s["truncated"]:
            notes.append(f"{s['truncated']} câu bị cắt")
        weak = [label for key, label in CHECKS
                if s["checks"][key]["applicable"] and s["checks"][key]["passed"] < s["checks"][key]["applicable"]]
        if weak:
            notes.append("trượt: " + ", ".join(weak))
        lines.append(
            f"| {bb._cell(m['model'])} | {_load_text(m['load'])} | {mem_text} | "
            f"{_num(s['tokens_per_s'])} | {_num(s['first_token_s'], suffix='s')} | "
            f"{_num(s['total_s'], suffix='s')} | {s['role_ok']}/{s['answered']} | "
            f"{bb._cell('; '.join(notes) or 'đạt hết')} |"
        )
    lines.append("")
    lines.append("Trung vị trên mọi câu; \"Giữ vai\" = số câu đạt mọi luật / số câu đã trả lời.")
    lines.append("")
    lines.append("**Giữ vai theo từng luật** (đạt/áp dụng)")
    lines.append("| Model | " + " | ".join(label for _key, label in CHECKS) + " |")
    lines.append("|---|" + "---|" * len(CHECKS))
    for m in models:
        s = m.get("summary")
        if not s or not s["answered"]:
            continue
        lines.append(f"| {bb._cell(m['model'])} | " + " | ".join(_ratio(s["checks"][k]) for k, _ in CHECKS) + " |")
    lines.append("")
    lines.append("**Ghi chú**")
    if result.get("interrupted"):
        lines.append("- Dừng giữa chừng bằng Ctrl+C: số trên chỉ gồm phần đã đo.")
    lines.append(f"- Số đo tự động: `python {result['command']}`; toàn bộ câu trả lời: "
                 f"`{result.get('files', {}).get('json', '?')}`.")
    lines.append("- Luật chỉ bắt lỗi thô; đọc câu trả lời trong file JSON rồi mới kết luận.")
    lines.append("- (điền kết luận: model nào giữ vai tự nhiên nhất, có đáng đổi OLLAMA_MODEL không)")
    lines.append("")
    lines.append("---")
    return "\n".join(lines) + "\n"


def plan_output_paths(out_dir: Path, stamp: str) -> tuple[Path, Path]:
    """Tên file chưa tồn tại — KHÔNG BAO GIỜ ghi đè kết quả cũ."""
    for n in range(1, 1000):
        suffix = "" if n == 1 else f"-{n}"
        json_path = out_dir / f"chat_{stamp}{suffix}.json"
        md_path = out_dir / f"chat_{stamp}{suffix}_testlog.md"
        if not json_path.exists() and not md_path.exists():
            return json_path, md_path
    raise RuntimeError(f"Quá nhiều file trùng thời điểm {stamp} trong {out_dir}")


# ══════════════════════════════════════════════════════════════════════════
# DÒNG LỆNH
# ══════════════════════════════════════════════════════════════════════════
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="bench_chat.py",
        description="So sánh model chat Ollama bằng cùng bộ hội thoại nhập vai: tốc độ, bộ nhớ, "
                    "giữ vai, tiếng Việt. Không tải model, không đụng DB/ComfyUI.",
    )
    parser.add_argument("--models", nargs="+", default=[OLLAMA_MODEL], metavar="MODEL",
                        help=f"model Ollama đã pull (mặc định OLLAMA_MODEL = {OLLAMA_MODEL})")
    parser.add_argument("--rounds", type=bb._int_in_range(1, MAX_ROUNDS, "--rounds"), default=DEFAULT_ROUNDS,
                        help=f"số vòng chạy lại cả bộ thoại, mỗi vòng một seed (mặc định {DEFAULT_ROUNDS})")
    parser.add_argument("--seed", type=bb._int_in_range(0, MAX_SEED - MAX_ROUNDS, "--seed"),
                        default=DEFAULT_SEED, help=f"seed vòng 1; vòng i dùng seed+i-1 (mặc định {DEFAULT_SEED})")
    parser.add_argument("--personas", nargs="+", choices=sorted(SCRIPTS), default=sorted(SCRIPTS),
                        help="nhân vật dùng để thử (mặc định: tất cả)")
    parser.add_argument("--timeout", type=bb._int_in_range(10, 1800, "--timeout"), default=CHAT_TIMEOUT_SEC,
                        help=f"giây chờ tối đa mỗi câu (mặc định = YUMEAI_CHAT_TIMEOUT_SEC = {CHAT_TIMEOUT_SEC})")
    parser.add_argument("--out-dir", default=os.path.join(DATA_DIR, "bench"),
                        help="thư mục ghi kết quả (mặc định <YUMEAI_DATA_DIR>/bench)")
    parser.add_argument("--no-free", action="store_true",
                        help="KHÔNG nhả model Ollama trước/sau mỗi model (khi đó số nạp cold có thể sai)")
    parser.add_argument("--check-only", action="store_true",
                        help="chỉ kiểm tra Ollama, model đã pull, ComfyUI, GPU/RAM — không chat")
    args = parser.parse_args(argv)
    args.models = list(dict.fromkeys(args.models))
    args.personas = [p for p in args.personas if p in PERSONAS]
    return args


def main(argv=None) -> int:
    bb._prefer_utf8_stdout()
    args = parse_args(argv)
    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d-%H%M%S")

    bb._section("Máy và dịch vụ")
    environment = {"platform": platform.platform(), "python": platform.python_version(),
                   "gpu": bb.query_gpus(), "ram": bb.read_ram()}
    gpu = (environment["gpu"].get("gpus") or [None])[0]
    print(f"  GPU: {gpu['name']} {gpu['total_mib']} MiB" if gpu else f"  GPU: {environment['gpu'].get('note')}")
    print(f"  RAM: {bb._fmt_gib(environment['ram'].get('total_mib'))} "
          f"(trống {bb._fmt_gib(environment['ram'].get('available_mib'))})")

    installed = installed_models()
    if installed is None:
        print(f"  ❌ Ollama không trả lời ở {OLLAMA_URL}. Mở `ollama serve` rồi chạy lại.")
        return 2
    print(f"  Ollama: chạy, đã pull {len(installed)} model: {', '.join(installed) or '(chưa có)'}")
    found, missing = [], []
    for name in args.models:
        resolved = resolve_model(name, installed)
        (found if resolved else missing).append(resolved or name)
    for name in missing:
        print(f"  ⚠️ Chưa có {name} — bỏ qua. Muốn thử thì tự chạy: ollama pull {name}")
    loaded = bb.ollama_loaded_models() or []
    if loaded:
        print("  Đang nạp: " + ", ".join(f"{m['name']} ({m['vram_mib']:.0f}/{m['size_mib']:.0f} MiB VRAM)" for m in loaded))

    comfy = bb.probe_comfy()
    services = {"ollama": {"url": OLLAMA_URL, "installed": installed, "loaded_at_start": loaded},
                "comfyui": comfy}
    if comfy.get("up"):
        queue = bb.comfy_queue_state()
        services["comfyui_queue"] = queue
        dev = (comfy.get("devices") or [{}])[0]
        print(f"  ComfyUI: đang chạy — VRAM trống theo ComfyUI {bb._fmt_mib(dev.get('vram_free_mib'), dev.get('vram_total_mib'))}")
        if queue and (queue[0] or queue[1]):
            print(f"  ❌ ComfyUI đang vẽ ({queue[0]} chạy, {queue[1]} chờ) — GPU bận nên số đo sẽ sai. "
                  "Đợi vẽ xong rồi chạy lại.")
            return 2
    else:
        print("  ComfyUI: tắt (tốt nhất cho phép đo này)")

    turns = sum(len(SCRIPTS[p]) for p in args.personas) * args.rounds
    print(f"  Kế hoạch: {len(found)} model × {turns} câu ({', '.join(PERSONAS[p]['name'] for p in args.personas)}, "
          f"{args.rounds} vòng)")
    if args.check_only:
        return 0 if found else 2
    if not found:
        print("  ❌ Không có model nào để đo.")
        return 2

    models, interrupted = [], False
    try:
        for name in found:
            run_model(name, personas=args.personas, rounds=args.rounds, seed=args.seed,
                      free=not args.no_free, timeout=args.timeout, sink=models)
    except KeyboardInterrupt:
        interrupted = True
        print("\n  ⏹ Dừng bằng Ctrl+C — ghi lại phần đã đo.")

    result = {
        "schema": BENCH_SCHEMA,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "interrupted": interrupted,
        "command": " ".join(["bench_chat.py", *(argv if argv is not None else sys.argv[1:])]),
        "args": {"models": args.models, "rounds": args.rounds, "seed": args.seed,
                 "personas": args.personas, "no_free": args.no_free, "timeout": args.timeout},
        "config": bench_config(),
        "environment": environment,
        "services": services,
        "missing": missing,
        "script": {"user_name": USER_NAME, "facts": BENCH_FACTS, "turns": {p: SCRIPTS[p] for p in args.personas}},
        "models": models,
    }
    for m in models:
        m.setdefault("summary", summarize_model(m))
    json_path, md_path = plan_output_paths(Path(args.out_dir), stamp)
    result["files"] = {"json": bb._display_path(json_path), "testlog": bb._display_path(md_path)}
    testlog = format_testlog_block(result)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    bb._write_new(json_path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    bb._write_new(md_path, testlog)

    bb._section("Khối dán vào docs/TESTLOG.md")
    print(testlog)
    print(f"Đã ghi {bb._display_path(json_path)} (kèm toàn bộ câu trả lời) và {bb._display_path(md_path)}.")
    if interrupted:
        return 130
    return 1 if any(m["errors"] for m in result["models"]) else 0


if __name__ == "__main__":
    sys.exit(main())
