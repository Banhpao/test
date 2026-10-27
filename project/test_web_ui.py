"""Test offline cho giao diện /app (chat-ui-mockup.html); không cần trình duyệt.

Giao diện là một file HTML viết tay nên không có trình biên dịch nào báo khi nó lệch
khỏi backend. Các test này chặn ba kiểu lệch hay gặp:
  - gọi một endpoint đã đổi tên/không tồn tại (UI hỏng mà không ai biết),
  - gửi chuỗi chất lượng/kiểu ảnh/phóng to khác hằng trong comfy_service (backend lặng
    lẽ dùng mặc định → người dùng chọn "Tốt nhất" mà ảnh vẽ ở mức khác),
  - chèn chữ của LLM/người dùng bằng innerHTML.
"""

import re
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
from yumeai.core.personas import PERSONAS
from yumeai.services import comfy_service

HTML_PATH = Path(__file__).with_name("chat-ui-mockup.html")
HTML = HTML_PATH.read_text(encoding="utf-8")


def _js_block(name: str) -> str:
    """Nội dung của `const NAME = [...]` hoặc `{...}` trong script."""
    match = re.search(rf"const {name} = ([\[{{].*?[\]}}])\n", HTML, re.S)
    if not match:
        raise AssertionError(f"Không tìm thấy const {name} trong chat-ui-mockup.html")
    return match.group(1)


def _values(name: str) -> list[str]:
    return re.findall(r"value: '([^']+)'", _js_block(name))


class WebUiContractTests(unittest.TestCase):
    def test_every_api_path_used_by_the_ui_exists(self):
        routes = {route.path for route in main.app.routes}
        used = set(re.findall(r"['`](/(?:api|health)[^'`?$]*)", HTML))
        self.assertTrue(used, "không đọc được đường dẫn API nào từ UI")
        for path in sorted(used):
            if path.endswith("/"):
                # `/api/memory/${...}` → phải có route dạng /api/memory/{fact_key}
                ok = any(r.startswith(path) and r[len(path):].startswith("{") for r in routes)
            else:
                ok = path in routes
            self.assertTrue(ok, f"UI gọi {path} nhưng main.py không có route đó")

    def test_image_urls_are_served(self):
        routes = {route.path for route in main.app.routes}
        self.assertIn("/generated", routes)

    def test_quality_values_match_backend_presets(self):
        self.assertEqual(sorted(_values("QUALITIES")), sorted(comfy_service.QUALITY_PRESETS))

    def test_upscale_values_match_backend(self):
        self.assertEqual(
            sorted(_values("UPSCALES")),
            sorted([comfy_service.UPSCALE_NONE, comfy_service.UPSCALE_ESRGAN_2X, comfy_service.UPSCALE_LATENT_15X]),
        )

    def test_style_values_match_backend(self):
        self.assertEqual(sorted(_values("STYLES")), sorted(comfy_service.STYLE_KEYWORDS))

    def test_default_quality_is_the_best_one(self):
        # Người dùng chọn: đẹp và chuẩn quan trọng hơn nhanh — mặc định là mức kỹ nhất.
        first = _values("QUALITIES")[0]
        best = max(comfy_service.QUALITY_PRESETS, key=lambda q: comfy_service.QUALITY_PRESETS[q]["steps"])
        self.assertEqual(first, best)

    def test_persona_tones_point_at_real_personas(self):
        tones = dict(re.findall(r"(\w+): '(rose|honey)'", _js_block("TONES")))
        self.assertTrue(tones)
        for persona_id in tones:
            self.assertIn(persona_id, PERSONAS)

    def test_innerhtml_only_for_constant_icons(self):
        uses = [line.strip() for line in HTML.splitlines() if re.search(r"\.innerHTML\s*=", line)]
        self.assertEqual(len(uses), 1, uses)
        self.assertIn("ICON_PATHS", uses[0])

    def test_app_route_serves_this_file(self):
        # Không dùng `with`: không chạy lifespan nên không đụng tới DB thật.
        response = TestClient(main.app).get("/app")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, HTML)


if __name__ == "__main__":
    unittest.main()
