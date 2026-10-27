"""
Test router + lịch sử chat — CHẠY OFFLINE, không cần Ollama hay ComfyUI.

    python test_router.py

Đây là lưới an toàn cho phần định tuyến: mỗi lần sửa keyword hay thêm rule mới,
chạy lại file này trước khi mở UI. Trước đây dự án không có test nào nên các lỗi
kiểu `"hi" in "khi nào"` chỉ lộ ra khi đang dùng thật.
"""
import sys

from yumeai.core.prompt_router import (
    contains_word,
    plan_request,
    rule_plan_request,
    looks_like_create,
)
from yumeai.core.chat_context import (
    build_chat_messages, build_persona_messages, clean_history, strip_ui_markers,
)
from yumeai.core.persona_prompt import build_system_prompt, build_fewshot_messages
from yumeai.core.personas import PERSONAS
from yumeai.services.comfy_service import ComfyUIError, comfy_is_up, ensure_comfy_ready, get_checkpoint_model
from yumeai.services import ollama_service
from yumeai.services.comfy_service import (
    normalize_upscale, normalize_quality,
    UPSCALE_NONE, UPSCALE_ESRGAN_2X, UPSCALE_LATENT_15X,
    QUALITY_BALANCED, QUALITY_FAST, QUALITY_PRESETS,
)
from yumeai.core.image_prompt import build_positive_prompt, NEGATIVE_PROMPT
from yumeai.core.runtime import comfy_job, ResourceBusyError

_fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}\n     got={got!r} want={want!r}" if not ok
          else f"✅ {label}  → {got!r}")
    if not ok:
        _fails.append(label)


def section(title):
    print(f"\n{'═' * 62}\n{title}\n{'═' * 62}")


# ══════════════════════════════════════════════════════════════════════════
section("1. Khớp theo ranh giới từ (bug: 'hi' khớp 'khi', 'đồ' khớp 'đồng')")
# ══════════════════════════════════════════════════════════════════════════
for text, kw, want in [
    ("khi nào em rảnh", "hi", False),
    ("chi tiết hơn đi", "hi", False),
    ("anh hiểu rồi", "hi", False),
    ("nhiều quá", "hi", False),
    ("hi anh", "hi", True),
    ("tôi đồng ý thay đổi kế hoạch", "đồ", False),
    ("đồng phục học sinh", "đồ", False),
    ("đổi thành đồ thể thao", "đồ", True),
    ("mặc đồ", "đồ", True),
]:
    check(f"contains_word({text!r}, {kw!r})", contains_word(text, kw), want)


# ══════════════════════════════════════════════════════════════════════════
section("2. Chat KHÔNG được biến thành lệnh tạo ảnh (bug nghiêm trọng nhất)")
# ══════════════════════════════════════════════════════════════════════════
CHAT_CASES = [
    "hôm nay bạn thế nào?",
    "anh nhớ em không",
    "em ăn cơm chưa",
    "kể chuyện đi",
    "bạn đang làm gì đấy",
    "chào em",
    "tôi buồn quá",
    "khi nào em rảnh",
]
for t in CHAT_CASES:
    plan = rule_plan_request(t, has_image=False)
    check(f"chat: {t!r}", plan.task, "chat")


# ══════════════════════════════════════════════════════════════════════════
section("3. Yêu cầu tạo ảnh THẬT vẫn phải ra text2img")
# ══════════════════════════════════════════════════════════════════════════
CREATE_CASES = [
    "tạo ảnh cô gái tóc bạc",
    "vẽ cho anh một bức tranh hoàng hôn",
    "vẽ em đi",
    "gửi ảnh của em đi",
    "cho anh xem hình em nào",
    "chụp cho anh tấm ảnh selfie",
    "generate an image of a girl",
    "draw a cat",
]
for t in CREATE_CASES:
    plan = rule_plan_request(t, has_image=False)
    check(f"text2img: {t!r}", plan.task, "text2img")


# ══════════════════════════════════════════════════════════════════════════
section("4. 'tạo/làm' KHÔNG kèm danh từ ảnh thì không phải lệnh vẽ")
# ══════════════════════════════════════════════════════════════════════════
for t in ["tạo động lực cho anh đi", "làm gì bây giờ", "em tạo tài khoản chưa"]:
    check(f"không phải create: {t!r}", looks_like_create(t)[0], False)


# ══════════════════════════════════════════════════════════════════════════
section("5. Có ảnh: chỉ đổi đồ khi thật sự nói về quần áo")
# ══════════════════════════════════════════════════════════════════════════
check("'tôi đồng ý thay đổi kế hoạch' + ảnh → KHÔNG phải edit_clothes",
      rule_plan_request("tôi đồng ý thay đổi kế hoạch", has_image=True).task != "edit_clothes",
      True)
check("'đổi sang đồ thể thao' + ảnh → edit_clothes",
      rule_plan_request("đổi sang đồ thể thao", has_image=True).task, "edit_clothes")
check("'xóa cái ly trên bàn' + ảnh → inpaint",
      rule_plan_request("xóa cái ly trên bàn", has_image=True).task, "inpaint")
check("có ảnh + có ảnh outfit → garment_transfer",
      rule_plan_request("mặc theo ảnh này", has_image=True, has_outfit_image=True).task,
      "garment_transfer")


# ══════════════════════════════════════════════════════════════════════════
section("6. plan_request (tắt LLM fallback) — chat vẫn là chat")
# ══════════════════════════════════════════════════════════════════════════
for t in CHAT_CASES:
    check(f"plan_request chat: {t!r}",
          plan_request(t, has_image=False, use_llm_fallback=False)["task"], "chat")


# ══════════════════════════════════════════════════════════════════════════
section("7. Lịch sử chat: giữ lời người dùng ở lượt CÓ ảnh")
# ══════════════════════════════════════════════════════════════════════════
check("bóc marker 📎, giữ câu thật",
      strip_ui_markers("📎 [ảnh hiện tại]\nem mặc cái này đẹp không"),
      "em mặc cái này đẹp không")
check("bóc cả 👕 và 🔎",
      strip_ui_markers("📎 [ảnh hiện tại]\nthử đồ thể thao đi\n"
                       "👕 [dùng ảnh trang phục tham chiếu]\n🔎 current=512x768/h123456"),
      "thử đồ thể thao đi")

history = [
    {"role": "user", "content": "chào em"},
    {"role": "assistant", "content": "H-hừ... chào anh."},
    {"role": "user", "content": "📎 [ảnh hiện tại]\nem thấy ảnh này sao?"},
    {"role": "assistant", "content": "✅ Xong!\n🧠 Task: edit_clothes"},
    {"role": "assistant", "content": "Baka! Đẹp đấy."},
]
cleaned = clean_history(history)
check("số message giữ lại (bỏ 1 message trạng thái ✅)", len(cleaned), 4)
check("lượt có ảnh KHÔNG bị mất", cleaned[2]["content"], "em thấy ảnh này sao?")

# Gradio 6 trả content dạng list khối {"type": "text"} — bản cũ bỏ sạch các lượt này.
g6_history = [
    {"role": "user", "content": [{"type": "text", "text": "chào em"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "H-hừ... chào anh."}]},
    {"role": "user", "content": [{"type": "text", "text": "📎 [ảnh hiện tại]\nem thấy ảnh này sao?"},
                                 {"type": "file", "file": {"path": "x.png"}}]},
    {"role": "assistant", "content": [{"type": "text", "text": "✅ Xong!\n🧠 Task: edit_clothes"}]},
]
check("Gradio 6: content dạng list khối text vẫn giữ được",
      clean_history(g6_history),
      [{"role": "user", "content": "chào em"},
       {"role": "assistant", "content": "H-hừ... chào anh."},
       {"role": "user", "content": "em thấy ảnh này sao?"}])

msgs = build_chat_messages(build_system_prompt(PERSONAS["pao"]), history, "nhớ anh không?")
check("message[0] là system", msgs[0]["role"], "system")
check("message cuối là câu mới", msgs[-1]["content"], "nhớ anh không?")
check("tổng số message = 1 system + 4 lịch sử + 1 mới", len(msgs), 6)

long_history = [{"role": "user", "content": f"câu số {i}"} for i in range(50)]
check("cửa sổ lịch sử bị cắt (không tràn context)",
      len(build_chat_messages("sys", long_history, "hi")) <= 14, True)


# ══════════════════════════════════════════════════════════════════════════
section("8. Persona")
# ══════════════════════════════════════════════════════════════════════════
check("có ít nhất 2 nhân vật", len(PERSONAS) >= 2, True)
REQUIRED = ("name", "age", "role", "personality", "speech_style",
            "boundaries", "greeting", "examples", "appearance")
for pid, p in PERSONAS.items():
    check(f"persona {pid!r} đủ field", all(k in p for k in REQUIRED), True)
    check(f"persona {pid!r} có few-shot", len(p["examples"]) >= 3, True)

pao = PERSONAS["pao"]
sp = build_system_prompt(pao)
check("system prompt dài hơn hẳn bản cũ (4 dòng)", len(sp) > 800, True)
for must in ("TÍNH CÁCH", "CÁCH NÓI CHUYỆN", "NGUYÊN TẮC BẮT BUỘC", "Pao"):
    check(f"system prompt có mục {must!r}", must in sp, True)
check("có ràng buộc chống lộ là AI", "không nói mình là AI" in sp.lower()
      or "TUYỆT ĐỐI không nói mình là AI" in sp, True)

sp_mem = build_system_prompt(pao, memory_summary="- Tên là Cenk\n- Đang học lập trình")
check("trí nhớ được nhét vào system prompt", "Cenk" in sp_mem, True)
check("không có trí nhớ thì không thêm mục thừa",
      "ĐÃ BIẾT VỀ NGƯỜI NÀY" not in sp, True)

fs = build_fewshot_messages(pao)
check("few-shot ra cặp user/assistant", len(fs) >= 6 and len(fs) % 2 == 0, True)
check("few-shot bắt đầu bằng user", fs[0]["role"], "user")

msgs = build_persona_messages(pao, [], "chào em", memory_summary="")
check("message[0] là system", msgs[0]["role"], "system")
check("message cuối là câu vừa gõ", msgs[-1]["content"], "chào em")
check("có chèn few-shot vào giữa", len(msgs) > 2, True)

# hồ sơ kiểu CŨ (chỉ có key "system") vẫn phải chạy được
legacy = {"name": "Old", "system": "Bạn là Old."}
check("tương thích persona kiểu cũ", build_system_prompt(legacy), "Bạn là Old.")


# ══════════════════════════════════════════════════════════════════════════
section("9. Degrade khi thiếu dịch vụ (tự bỏ qua nếu dịch vụ đang chạy)")
# ══════════════════════════════════════════════════════════════════════════
if comfy_is_up():
    print("⏭️  ComfyUI ĐANG CHẠY → bỏ qua nhóm test 'ComfyUI tắt'")
    try:
        m = get_checkpoint_model("Anime")
        check("ComfyUI đang chạy → chọn được checkpoint", bool(m), True)
    except ComfyUIError as e:
        print(f"⚠️  ComfyUI chạy nhưng chưa có checkpoint: {e}")
else:
    err = None
    try:
        ensure_comfy_ready("test")
    except ComfyUIError as e:
        err = str(e)
    check("ComfyUI tắt → ensure_comfy_ready ném ComfyUIError", err is not None, True)
    check("thông báo nói rõ phải bật ComfyUI", "python main.py --lowvram" in (err or ""), True)

    err2 = None
    try:
        get_checkpoint_model("Anime")
    except ComfyUIError as e:
        err2 = str(e)
    check("ComfyUI tắt → get_checkpoint_model ném lỗi rõ ràng (không trả '')",
          err2 is not None and "ComfyUI" in err2, True)

# Ollama
try:
    import httpx as _hx
    with _hx.Client(timeout=2) as _c:
        ollama_up = _c.get(f"{ollama_service.OLLAMA_URL}/api/tags").status_code == 200
except Exception:
    ollama_up = False

if ollama_up:
    print("⏭️  Ollama ĐANG CHẠY → bỏ qua nhóm test 'Ollama tắt'")
    en, ok = ollama_service.translate_vi_en_ex("cô gái mặc áo dài đỏ")
    check("Ollama chạy → dịch thành công", ok, True)
    check("model dịch đã được cache", ollama_service._TRANSLATE_MODEL_CACHE is not None, True)
else:
    vi = "cô gái mặc áo dài đỏ đứng dưới mưa"
    en, ok = ollama_service.translate_vi_en_ex(vi)
    check("Ollama tắt → translate_vi_en_ex báo ok=False", ok, False)
    check("Ollama tắt → giữ nguyên văn bản gốc", en, vi)
    ollama_service.get_translate_model()
    check("Ollama tắt → KHÔNG cache model đoán bừa (bật Ollama sau vẫn dò lại)",
          ollama_service._TRANSLATE_MODEL_CACHE, None)

check("không còn hằng TRANSLATE_MODEL chốt lúc import",
      hasattr(ollama_service, "TRANSLATE_MODEL"), False)

# ══════════════════════════════════════════════════════════════════════════
section("10. Preset upscale/quality: mọi cách viết quy về một mối")
# ══════════════════════════════════════════════════════════════════════════
for raw, want in [
    ("Không", UPSCALE_NONE),                 # main.py cũ
    ("Không upscale", UPSCALE_NONE),         # AUTO_PRESETS cũ
    ("", UPSCALE_NONE),
    ("2x (ESRGAN)", UPSCALE_ESRGAN_2X),
    ("2x", UPSCALE_ESRGAN_2X),
    ("1.5x (Latent)", UPSCALE_LATENT_15X),
    ("gõ sai lung tung", UPSCALE_NONE),      # phải CẢNH BÁO, không im lặng
]:
    check(f"normalize_upscale({raw!r})", normalize_upscale(raw), want)

for raw, want in [
    ("Cân bằng (28 steps)", QUALITY_BALANCED),
    ("nhanh", QUALITY_FAST),
    ("28", QUALITY_BALANCED),
    ("không tồn tại", QUALITY_BALANCED),
]:
    check(f"normalize_quality({raw!r})", normalize_quality(raw), want)

check("mọi key quality đều có preset",
      all(k in QUALITY_PRESETS for k in (QUALITY_FAST, QUALITY_BALANCED)), True)


# ══════════════════════════════════════════════════════════════════════════
section("11. Prompt text2img dùng chung cho cả hai entrypoint")
# ══════════════════════════════════════════════════════════════════════════
pp = build_positive_prompt("red ao dai, smiling", "Anime", "1girl, solo, silver hair")
check("ngoại hình nhân vật đứng ĐẦU prompt", pp.startswith("1girl, solo, silver hair"), True)
check("có nội dung người dùng yêu cầu", "red ao dai" in pp, True)
check("có style tag (main.py cũ thiếu hẳn)", "anime style" in pp, True)
check("có đuôi chất lượng", "masterpiece" in pp, True)
check("không ngoại hình thì không có dấu phẩy thừa ở đầu",
      build_positive_prompt("a cat", "Anime").startswith("a cat"), True)
check("negative gộp cả 'jpeg artifacts' (app_gradio cũ)", "jpeg artifacts" in NEGATIVE_PROMPT, True)
check("negative gộp cả 'mutated hands' (main.py cũ)", "mutated hands" in NEGATIVE_PROMPT, True)


# ══════════════════════════════════════════════════════════════════════════
section("12. Resource Guard: nhả slot kể cả khi giữa chừng có lỗi")
# ══════════════════════════════════════════════════════════════════════════
try:
    with comfy_job("test-nổ"):
        raise RuntimeError("giả lập lỗi giữa submit và poll")
except RuntimeError:
    pass

second_ok = True
try:
    with comfy_job("test-sau"):
        pass
except ResourceBusyError:
    second_ok = False
check("job lỗi xong vẫn lấy được slot mới (không kẹt 'đang bận')", second_ok, True)

with comfy_job("ngoài"):
    nested_ok = True
    try:
        with comfy_job("trong"):   # cùng thread → phải cho lồng nhau
            pass
    except ResourceBusyError:
        nested_ok = False
check("cho phép lồng slot trong cùng một luồng (pipeline 2-pass)", nested_ok, True)

after_nested = True
try:
    with comfy_job("sau-lồng"):
        pass
except ResourceBusyError:
    after_nested = False
check("thoát khỏi lồng thì slot được nhả hết", after_nested, True)

# ══════════════════════════════════════════════════════════════════════════
section("13. Chat không ảnh: KHÔNG gọi planner LLM trừ khi câu nhắc tới ảnh")
# ══════════════════════════════════════════════════════════════════════════
import os as _os
import subprocess as _subprocess
from unittest.mock import patch as _patch

from yumeai.core import config as _config
from yumeai.core import prompt_router as _router
from yumeai.core import runtime as _runtime
from yumeai.core.chat_context import chat_options as _chat_options
from yumeai.services import comfy_service as _comfy


class _FakeResp:
    def __init__(self, data=None, status=200):
        self._data, self.status_code = data or {}, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.Client giả: ghi lại mọi request, trả lời theo bảng `routes`."""
    calls = []
    routes = {}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _answer(self, method, url, json=None):
        _FakeClient.calls.append((method, url, json))
        for suffix, resp in _FakeClient.routes.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResp({}, 404)

    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, json=None, **kw):
        return self._answer("POST", url, json)


def _fake_http(routes):
    _FakeClient.calls = []
    _FakeClient.routes = routes
    return _patch("httpx.Client", _FakeClient)


# Bản cũ: 9/10 câu chat thường ra confidence="low" → mỗi lượt chờ planner LLM (25s).
for t in CHAT_CASES + ["ừ", "haha", "anh nhớ em", "hình như em buồn",
                       "chuyện đó ảnh hưởng tới anh nhiều lắm", "tình hình công việc sao rồi"]:
    check(f"không ảnh, không cần planner: {t!r}",
          rule_plan_request(t, has_image=False).confidence, "high")
for t in ["em có ảnh nào đẹp không", "hôm qua chụp ở đâu thế"]:
    check(f"có nhắc ảnh → vẫn nhờ planner soi lại: {t!r}",
          rule_plan_request(t, has_image=False).confidence, "low")

_planner_calls = []
with _patch.object(_router, "_llm_plan_request",
                   side_effect=lambda **kw: _planner_calls.append(kw) or None):
    for t in ["anh nhớ em", "ừ", "haha", "em ăn cơm chưa"]:
        plan_request(t, has_image=False, use_llm_fallback=True)
check("plan_request: chat thường không gọi planner lần nào", len(_planner_calls), 0)

with _fake_http({"/api/chat": _FakeResp({"message": {"content": '{"task": "chat"}'}})}):
    _router._llm_plan_request("em có ảnh nào đẹp không", False, False)
_payload = _FakeClient.calls[-1][2]
check("planner gửi think=false", _payload.get("think"), False)
check("planner vẫn ép format json", _payload.get("format"), "json")


# ══════════════════════════════════════════════════════════════════════════
section("14. Model: mặc định khớp docs, dò cỡ model bằng regex, env override")
# ══════════════════════════════════════════════════════════════════════════
_env = {k: v for k, v in _os.environ.items()
        if k not in ("OLLAMA_MODEL", "YUMEAI_VISION_MODEL", "YUMEAI_TRANSLATE_MODEL")}
_out = _subprocess.run(
    [sys.executable, "-c",
     "from yumeai.core.config import OLLAMA_MODEL as m; print(m)"],
    capture_output=True, text=True, env=_env, cwd=_os.path.dirname(_os.path.abspath(__file__)),
    check=False,
)
check("model chat mặc định = qwen3:4b-instruct (như env.example/RUNBOOK, D24)", _out.stdout.strip(),
      "qwen3:4b-instruct")

for name, want in [
    ("llama3.2:1b", True), ("qwen2.5:0.5b", True), ("qwen2.5:1.5b", True),
    ("deepseek-r1:1.5b", True), ("tinyllama", True), ("smollm2:135m", True),
    ("llama3.2-vision:11b", False),    # bản cũ: "1b" in name → bị coi là tí hon
    ("qwen2.5:3b", False), ("qwen2.5:14b", False), ("llama3.1:8b", False),
    ("gemma2:2b", False), ("qwen2.5:7b-instruct-q4_K_M", False), ("qwen3:4b-instruct", False),
]:
    check(f"is_tiny_model({name!r})", ollama_service.is_tiny_model(name), want)

with _patch.object(ollama_service, "OLLAMA_TRANSLATE_MODEL", "my-translate:7b"), \
        _fake_http({}):
    check("YUMEAI_TRANSLATE_MODEL thắng việc tự dò", ollama_service.get_translate_model(),
          "my-translate:7b")
    check("đã chỉ định model dịch thì không hỏi /api/tags", _FakeClient.calls, [])

with _patch.object(ollama_service, "OLLAMA_TRANSLATE_MODEL", "qwen2.5:3b"), \
        _fake_http({"/api/chat": _FakeResp({"message": {"content": "red ao dai"}})}):
    ollama_service.translate_vi_en_ex("áo dài đỏ")
check("dịch gửi think=false", _FakeClient.calls[-1][2].get("think"), False)


# ══════════════════════════════════════════════════════════════════════════
section("15. VRAM: Ollama và ComfyUI nhường GPU 4GB cho nhau")
# ══════════════════════════════════════════════════════════════════════════
_ps = _FakeResp({"models": [
    {"name": "qwen2.5:3b", "model": "qwen2.5:3b", "size_vram": 2_000_000_000},
    {"name": "nomic-embed-text:latest", "model": "nomic-embed-text:latest", "size_vram": 0},
]})
with _patch.object(ollama_service, "VRAM_HANDOFF", True), \
        _fake_http({"/api/ps": _ps, "/api/generate": _FakeResp({"done_reason": "unload"})}):
    _unloaded = ollama_service.unload_ollama_models()
_gen = [c for c in _FakeClient.calls if c[1].endswith("/api/generate")]
check("chỉ nhả model đang nằm trên GPU (size_vram > 0)", _unloaded, ["qwen2.5:3b"])
check("nhả bằng keep_alive=0", _gen and _gen[0][2], {"model": "qwen2.5:3b", "keep_alive": 0})

with _patch.object(ollama_service, "VRAM_HANDOFF", False), _fake_http({"/api/ps": _ps}):
    check("YUMEAI_VRAM_HANDOFF=0 → không đụng tới Ollama", ollama_service.unload_ollama_models(), [])
    check("YUMEAI_VRAM_HANDOFF=0 → không có request nào", _FakeClient.calls, [])

with _patch.object(ollama_service, "VRAM_HANDOFF", True), \
        _fake_http({"/api/ps": RuntimeError("Ollama tắt")}):
    check("Ollama tắt → không ném lỗi, không chặn job vẽ", ollama_service.unload_ollama_models(), [])

with _patch.object(_comfy, "VRAM_HANDOFF", True), _patch.object(_comfy, "OLLAMA_NUM_GPU", None), \
        _fake_http({"/free": _FakeResp({})}):
    check("free_comfy_vram → True", _comfy.free_comfy_vram(), True)
check("ComfyUI /free nhận đúng payload",
      _FakeClient.calls[-1][1:], (f"{_comfy.COMFY_URL}/free", {"unload_models": True, "free_memory": True}))

with _patch.object(_comfy, "VRAM_HANDOFF", True), _patch.object(_comfy, "OLLAMA_NUM_GPU", 0), \
        _fake_http({"/free": _FakeResp({})}):
    _comfy.free_comfy_vram()
    check("Ollama chạy CPU (num_gpu=0) → không cần đuổi ComfyUI", _FakeClient.calls, [])

with _patch.object(_comfy, "VRAM_HANDOFF", True), _patch.object(_comfy, "OLLAMA_NUM_GPU", None), \
        _fake_http({"/free": RuntimeError("ComfyUI tắt")}):
    check("ComfyUI tắt → free_comfy_vram không ném lỗi", _comfy.free_comfy_vram(), False)

_order = []
with _patch.object(_runtime, "unload_ollama_models", lambda: _order.append("unload")), \
        _patch.object(_runtime, "_raw_submit_workflow", lambda wf: _order.append("submit") or "pid"), \
        _patch.object(_runtime, "_raw_poll_and_download",
                      lambda *a, **kw: _order.append("poll") or "img"):
    _img = _runtime.run_comfy_job({"1": {}}, None, 0.0, 5)
check("run_comfy_job: nhả Ollama TRƯỚC khi gửi job", _order, ["unload", "submit", "poll"])

with _patch.object(_config, "OLLAMA_NUM_GPU", None):
    check("YUMEAI_OLLAMA_NUM_GPU trống → không gửi num_gpu", "num_gpu" in _chat_options(), False)
with _patch.object(_config, "OLLAMA_NUM_GPU", 0):
    check("YUMEAI_OLLAMA_NUM_GPU=0 → chat gửi num_gpu=0", _chat_options().get("num_gpu"), 0)
    with _fake_http({"/api/chat": _FakeResp({"message": {"content": '{"task": "chat"}'}})}):
        ollama_service._ollama_chat("m", "sys", "user", retries=0)
        _router._llm_plan_request("em có ảnh nào đẹp không", False, False)
    check("num_gpu đi kèm MỌI lời gọi (khác nhau là Ollama nạp lại model)",
          [c[2]["options"].get("num_gpu") for c in _FakeClient.calls], [0, 0])

# ══════════════════════════════════════════════════════════════════════════
print(f"\n{'═' * 62}")
if _fails:
    print(f"❌ HỎNG {len(_fails)} test:")
    for f in _fails:
        print(f"   - {f}")
    sys.exit(1)
print("✅ TẤT CẢ TEST ĐỀU PASS")
