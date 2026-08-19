"""
devlog — ghi nhanh một dòng vào docs/CHANGELOG.md.

    python devlog.py "Thêm bảng conversation vào SQLite"
    python devlog.py                    # không tham số → mở chế độ nhập nhiều dòng
    python devlog.py --show             # xem 30 dòng đầu của changelog

Mục đích: giữ cho bộ nhớ dự án luôn cập nhật mà không tốn công. Người (hoặc AI)
tiếp theo mở dự án ra là nắm được ngay đã có chuyện gì xảy ra.

QUYẾT ĐỊNH THIẾT KẾ thì đừng ghi ở đây — ghi vào docs/DECISIONS.md, vì đó mới là
thứ ngăn người sau đi lại con đường đã bị loại.
"""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"
ANCHOR = "---\n"


def read_entry_from_args() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        return " ".join(args).strip()
    print("Nhập nội dung thay đổi (mỗi dòng một ý, dòng trống để kết thúc):")
    lines = []
    while True:
        try:
            line = input("  - ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        lines.append(line)
    return "\n".join(f"- {x}" for x in lines)


def show():
    if not CHANGELOG.exists():
        print(f"❌ Chưa có {CHANGELOG}")
        return
    text = CHANGELOG.read_text(encoding="utf-8")
    print("\n".join(text.splitlines()[:30]))


def append(entry: str):
    if not entry:
        print("Không có nội dung, bỏ qua.")
        return
    if not CHANGELOG.exists():
        print(f"❌ Không tìm thấy {CHANGELOG}")
        return

    body = entry if entry.lstrip().startswith("-") else f"- {entry}"
    block = f"## {date.today().isoformat()}\n\n{body}\n\n---\n\n"

    text = CHANGELOG.read_text(encoding="utf-8")
    idx = text.find(ANCHOR)
    if idx == -1:
        text = text.rstrip() + "\n\n---\n\n" + block
    else:
        cut = idx + len(ANCHOR)
        text = text[:cut] + "\n" + block + text[cut:].lstrip("\n")

    CHANGELOG.write_text(text, encoding="utf-8")
    print(f"✅ Đã ghi vào {CHANGELOG.relative_to(ROOT)}:\n{body}")


if __name__ == "__main__":
    if "--show" in sys.argv:
        show()
    else:
        append(read_entry_from_args())
