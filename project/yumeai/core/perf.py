"""Số đo hiệu năng THẬT của từng lượt chat và từng lần vẽ (lộ trình P3.5).

bench_baseline.py đo một lần, trong điều kiện dựng sẵn. File này đo lúc dùng thật: mỗi lượt
chat qua /api/chat và mỗi ảnh qua /api/images/generate nối MỘT dòng JSON vào
<YUMEAI_DATA_DIR>/perf.jsonl. GET /api/stats đọc lại vài trăm dòng gần nhất và trả trung vị,
để biết máy này thật sự chạy bao nhanh (token/giây, chờ chữ đầu tiên, mấy phút một ảnh "Tốt
nhất") và để so model với nhau bằng số chứ không bằng cảm giác.

Chỉ ghi SỐ và tên preset/model: thời gian, số token, kiểu ảnh, chất lượng. KHÔNG ghi nội dung
tin nhắn, prompt, tên người dùng hay user_id. Nhưng mỗi dòng có giờ đo, tức là cho biết lúc nào
có người chat/vẽ — nên "Xoá toàn bộ dữ liệu" xoá luôn sổ này (PerfLog.clear). Sổ không vào file
xuất dữ liệu và không vào bản sao lưu (đo lại được, không đáng giữ). Tắt hẳn:
    $env:YUMEAI_PERF_LOG="0"

Ghi là best-effort: lỗi ghi file chỉ in cảnh báo MỘT lần rồi bỏ qua, không bao giờ làm hỏng
lượt chat hay ảnh đang trả về. File tự xoay vòng khi quá ~2 MB (giữ đúng một bản cũ .1), nên
không phình mãi trên máy dùng lâu.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from yumeai.core.config import DATA_DIR

PERF_LOG_ENABLED = os.getenv("YUMEAI_PERF_LOG", "1").strip().lower() not in {"0", "false", "off", "no"}
PERF_LOG_PATH = Path(DATA_DIR) / "perf.jsonl"

SCHEMA = 1
MAX_BYTES = 2_000_000      # ~10.000 dòng; quá mức này thì đổi thành perf.jsonl.1
SUMMARY_WINDOW = 500       # /api/stats chỉ tính trên chừng này lần đo gần nhất

_NS = 1_000_000_000        # Ollama trả mọi thời lượng bằng nano giây


def _number(value):
    """Số thật (int/float, không phải bool) hoặc None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def ollama_chunk_stats(chunk: dict) -> dict:
    """Rút số đo từ chunk CUỐI (done=true) của Ollama /api/chat.

    eval_* là phần sinh câu trả lời (token/giây người dùng cảm nhận được); prompt_eval_* là
    phần đọc prompt — dài ra khi lịch sử và trí nhớ dài ra; load_duration > 0 nghĩa là Ollama
    phải nạp lại model (thường do VRAM vừa nhường cho ComfyUI). done_reason "length" nghĩa là
    câu trả lời bị cắt vì chạm num_predict.
    """
    out: dict = {}
    output_tokens = _number(chunk.get("eval_count"))
    eval_ns = _number(chunk.get("eval_duration"))
    if output_tokens is not None:
        out["output_tokens"] = int(output_tokens)
        if output_tokens > 0 and eval_ns:
            out["tokens_per_s"] = round(output_tokens / (eval_ns / _NS), 2)
    prompt_tokens = _number(chunk.get("prompt_eval_count"))
    prompt_ns = _number(chunk.get("prompt_eval_duration"))
    if prompt_tokens is not None:
        out["prompt_tokens"] = int(prompt_tokens)
        if prompt_tokens > 0 and prompt_ns:
            out["prompt_tokens_per_s"] = round(prompt_tokens / (prompt_ns / _NS), 1)
    load_ns = _number(chunk.get("load_duration"))
    if load_ns is not None:
        out["load_s"] = round(load_ns / _NS, 3)
    total_ns = _number(chunk.get("total_duration"))
    if total_ns is not None:
        out["ollama_total_s"] = round(total_ns / _NS, 3)
    reason = chunk.get("done_reason")
    if isinstance(reason, str) and reason:
        out["done_reason"] = reason[:20]
    return out


def _spread(values) -> dict | None:
    values = [v for v in values if _number(v) is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


class PerfLog:
    """Sổ đo hiệu năng dạng JSON Lines. Một instance dùng chung cho cả tiến trình."""

    def __init__(self, path, *, enabled: bool = True, max_bytes: int = MAX_BYTES):
        self.path = Path(path)
        self.enabled = enabled
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._warned = False

    @property
    def rotated_path(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    # ── Ghi ─────────────────────────────────────────────────
    def record(self, kind: str, **fields) -> dict | None:
        """Nối một lần đo. Trả về dòng đã ghi, hoặc None khi tắt/ghi lỗi.

        Trường có giá trị None bị bỏ để file gọn và để summarize() không phải đoán.
        """
        if not self.enabled:
            return None
        row = {"v": SCHEMA, "ts": datetime.now().astimezone().isoformat(timespec="seconds"), "kind": kind}
        row.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if self.path.stat().st_size + len(line) > self.max_bytes:
                        os.replace(self.path, self.rotated_path)
                except FileNotFoundError:
                    pass
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                print(f"⚠️ Không ghi được số đo hiệu năng vào {self.path}: {exc} — bỏ qua, app vẫn chạy.")
            return None
        return row

    def clear(self) -> int:
        """Xoá sổ (cả bản .1). Trả về số file đã xoá.

        Gọi từ "Xoá toàn bộ dữ liệu": sổ không có nội dung nhưng có GIỜ từng lượt chat/vẽ,
        tức là lịch sử dùng app — xoá hết dữ liệu mà còn để lại cái này là không thật lòng.
        """
        removed = 0
        with self._lock:
            for path in (self.path, self.rotated_path):
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed

    # ── Đọc ─────────────────────────────────────────────────
    def records(self, limit: int = SUMMARY_WINDOW) -> list[dict]:
        """limit lần đo gần nhất, cũ trước mới sau. Dòng hỏng (mất điện giữa lúc ghi) bị bỏ qua."""
        tail: deque = deque(maxlen=max(1, limit))
        for path in (self.rotated_path, self.path):
            try:
                with open(path, encoding="utf-8") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and isinstance(row.get("kind"), str):
                            tail.append(row)
            except FileNotFoundError:
                continue
            except OSError:
                continue
        return list(tail)

    def summarize(self, limit: int = SUMMARY_WINDOW) -> dict:
        rows = self.records(limit)
        return {
            "enabled": self.enabled,
            "window": limit,
            "measurements": len(rows),
            "since": rows[0]["ts"] if rows else None,
            "chat": _summarize_chat([r for r in rows if r["kind"] == "chat"]),
            "image": _summarize_images([r for r in rows if r["kind"] == "image"]),
        }


def _latest_first(rows: list[dict], key) -> list[tuple]:
    """Nhóm rows (cũ trước mới sau) theo key; nhóm có lần đo MỚI NHẤT đứng đầu.

    Xếp theo vị trí trong sổ chứ không theo ts: ts chỉ chính xác tới giây nên hai lần đo
    liền nhau có thể trùng ts.
    """
    groups: dict = {}
    last: dict = {}
    for index, row in enumerate(rows):
        k = key(row)
        groups.setdefault(k, []).append(row)
        last[k] = index
    return sorted(groups.items(), key=lambda item: last[item[0]], reverse=True)


def _summarize_chat(rows: list[dict]) -> dict:
    models = []
    # Model dùng gần nhất lên đầu: đó là model đang cấu hình.
    for model, items in _latest_first(rows, lambda r: str(r.get("model") or "?")):
        ok = [r for r in items if r.get("status") == "ok"]
        models.append({
            "model": model,
            "turns": len(items),
            "ok": len(ok),
            "failed": sum(1 for r in items if r.get("status") in {"error", "incomplete"}),
            "cancelled": sum(1 for r in items if r.get("status") == "cancelled"),
            "truncated": sum(1 for r in ok if r.get("done_reason") == "length"),
            "reloads": sum(1 for r in ok if (_number(r.get("load_s")) or 0) >= 1.0),
            "tokens_per_s": _spread(r.get("tokens_per_s") for r in ok),
            "first_token_s": _spread(r.get("first_token_s") for r in ok),
            "total_s": _spread(r.get("total_s") for r in ok),
            "prompt_tokens": _spread(r.get("prompt_tokens") for r in ok),
            "last_at": items[-1]["ts"],
        })
    return {"turns": len(rows), "models": models}


def _preset_key(row: dict) -> tuple[str, str]:
    return str(row.get("quality") or "?"), str(row.get("upscale") or "?")


def _summarize_images(rows: list[dict]) -> dict:
    presets = []
    for (quality, upscale), items in _latest_first(rows, _preset_key):
        ok = [r for r in items if r.get("status") == "ok"]
        presets.append({
            "quality": quality,
            "upscale": upscale,
            "count": len(items),
            "ok": len(ok),
            "failed": sum(1 for r in items if r.get("status") == "error"),
            "busy": sum(1 for r in items if r.get("status") == "busy"),
            "total_s": _spread(r.get("total_s") for r in ok),
            "comfy_s": _spread(r.get("comfy_s") for r in ok),
            "translate_s": _spread(r.get("translate_s") for r in ok),
            "models": sorted({str(r["model"]) for r in ok if r.get("model")}),
            "last_at": items[-1]["ts"],
        })
    return {"count": len(rows), "presets": presets}


perf_log = PerfLog(PERF_LOG_PATH, enabled=PERF_LOG_ENABLED)
