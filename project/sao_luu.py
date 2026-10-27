"""
sao_luu — sao lưu và khôi phục toàn bộ dữ liệu YumeAI (DB + ảnh) trong một file zip.

    python sao_luu.py                          # tạo một bản sao lưu mới
    python sao_luu.py --danh-sach              # xem các bản sao lưu đang có
    python sao_luu.py --khoi-phuc <file.zip>   # khôi phục (hỏi lại trước khi đè)
    python sao_luu.py --khoi-phuc <file.zip> --dong-y   # khôi phục, không hỏi lại

Bản sao lưu nằm ở <YUMEAI_DATA_DIR, mặc định project/data>/backups/sao-luu-<thời điểm>.zip:
  yumeai.db       chụp bằng backup API của sqlite — đúng cả khi main.py đang chạy (WAL),
                  khác với copy file .db có thể thiếu những trang còn nằm trong file -wal
  images/         mọi ảnh trong thư mục ảnh của gallery
  manifest.json   thời điểm, phiên bản schema, số hội thoại/tin nhắn/ký ức/ảnh

Khôi phục:
  - TỪ CHỐI khi main.py (cổng 8000) hoặc app_gradio.py (cổng 7860) đang chạy: thay DB dưới
    chân một tiến trình đang mở nó là cách nhanh nhất để làm hỏng dữ liệu.
  - Kiểm tra bản sao lưu trước khi đụng vào gì: có yumeai.db, PRAGMA integrity_check = ok,
    schema không mới hơn bản code đang chạy (DB của bản mới hơn thì code này đọc sai).
  - Luôn sao lưu dữ liệu HIỆN TẠI trước (truoc-khoi-phuc-<thời điểm>.zip), rồi mới thay DB và
    thư mục ảnh. Khôi phục nhầm thì khôi phục lại từ file đó.
  - Xoá file -wal/-shm cũ của DB trước khi đặt DB mới vào: để lại thì SQLite sẽ đem nhật ký
    của DB cũ áp lên DB mới.

Chỉ dùng thư viện chuẩn, chạy được không cần venv.
Mã thoát: 0 = xong; 1 = lỗi; 2 = từ chối (app đang chạy, bản sao lưu hỏng, người dùng thôi).
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from yumeai.core.config import DATA_DIR, DATABASE_PATH, GENERATED_IMAGES_DIR
from yumeai.storage.database import SCHEMA_VERSION

APP_PORTS = (8000, 7860)          # main.py, app_gradio.py
DB_NAME_IN_ZIP = "yumeai.db"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


class Refused(Exception):
    """Không làm vì không an toàn — mã thoát 2."""


def default_backup_dir() -> Path:
    return Path(DATA_DIR) / "backups"


def running_apps(ports=APP_PORTS, host: str = "127.0.0.1") -> list[int]:
    """Cổng nào đang có tiến trình nghe — tức app đang mở DB."""
    busy = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                busy.append(port)
    return busy


def _snapshot_db(db_path: Path, target: Path) -> None:
    source = sqlite3.connect(str(db_path))
    try:
        copy = sqlite3.connect(str(target))
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def _db_stats(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        stats = {"schema_version": conn.execute("PRAGMA user_version").fetchone()[0]}
        for table in ("users", "conversations", "messages", "memory_facts", "images"):
            try:
                stats[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.DatabaseError:
                stats[table] = None
        return stats
    finally:
        conn.close()


def _image_files(images_dir: Path) -> list[Path]:
    if not images_dir.is_dir():
        return []
    return sorted(p for p in images_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def create_backup(
    db_path: Path | str = DATABASE_PATH,
    images_dir: Path | str = GENERATED_IMAGES_DIR,
    backup_dir: Path | str | None = None,
    *,
    label: str = "sao-luu",
) -> Path:
    """Tạo file zip sao lưu; trả đường dẫn của nó."""
    db_path, images_dir = Path(db_path), Path(images_dir)
    backup_dir = Path(backup_dir) if backup_dir else default_backup_dir()
    if not db_path.exists():
        raise FileNotFoundError(f"Chưa có DB ở {db_path} — chưa có gì để sao lưu.")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{label}-{stamp}.zip"
    n = 2
    while target.exists():
        target = backup_dir / f"{label}-{stamp}-{n}.zip"
        n += 1

    images = _image_files(images_dir)
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / DB_NAME_IN_ZIP
        _snapshot_db(db_path, snapshot)
        manifest = {
            "app": "YumeAI",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source_db": str(db_path),
            **_db_stats(snapshot),
            "image_files": len(images),
        }
        partial = target.with_suffix(".zip.part")
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, DB_NAME_IN_ZIP)
            for image in images:
                # Ảnh đã nén sẵn, nén thêm chỉ tốn thời gian.
                zf.write(image, f"images/{image.name}", compress_type=zipfile.ZIP_STORED)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # Đổi tên sau cùng: mất điện giữa chừng thì chỉ còn file .part, không có một
        # "bản sao lưu" thiếu nửa nằm lẫn trong danh sách.
        partial.replace(target)
    return target


def list_backups(backup_dir: Path | str | None = None) -> list[dict]:
    backup_dir = Path(backup_dir) if backup_dir else default_backup_dir()
    rows = []
    for path in sorted(backup_dir.glob("*.zip")) if backup_dir.is_dir() else []:
        row = {"path": path, "size": path.stat().st_size, "manifest": None}
        try:
            with zipfile.ZipFile(path) as zf:
                if "manifest.json" in zf.namelist():
                    row["manifest"] = json.loads(zf.read("manifest.json").decode("utf-8"))
        except (zipfile.BadZipFile, ValueError, OSError):
            row["manifest"] = {"error": "file hỏng"}
        rows.append(row)
    return rows


def inspect_backup(zip_path: Path | str) -> dict:
    """Kiểm bản sao lưu trước khi khôi phục; ném Refused nếu không dùng được."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise Refused(f"Không thấy file {zip_path}")
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise Refused(f"{zip_path.name} không phải file zip hợp lệ.")
    with zf, tempfile.TemporaryDirectory() as tmp:
        if DB_NAME_IN_ZIP not in zf.namelist():
            raise Refused(f"{zip_path.name} không có {DB_NAME_IN_ZIP} — không phải bản sao lưu YumeAI.")
        extracted = Path(tmp) / DB_NAME_IN_ZIP
        extracted.write_bytes(zf.read(DB_NAME_IN_ZIP))
        try:
            conn = sqlite3.connect(str(extracted))
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError as e:
            raise Refused(f"DB trong bản sao lưu hỏng: {e}")
        if check != "ok":
            raise Refused(f"DB trong bản sao lưu không qua integrity_check: {check}")
        stats = _db_stats(extracted)
        if (stats["schema_version"] or 0) > SCHEMA_VERSION:
            raise Refused(
                f"Bản sao lưu dùng schema v{stats['schema_version']}, mới hơn bản code này "
                f"(v{SCHEMA_VERSION}). Cập nhật code rồi hãy khôi phục."
            )
        stats["image_files"] = sum(
            1 for name in zf.namelist()
            if name.startswith("images/") and Path(name).suffix.lower() in IMAGE_SUFFIXES
        )
        return stats


def restore_backup(
    zip_path: Path | str,
    db_path: Path | str = DATABASE_PATH,
    images_dir: Path | str = GENERATED_IMAGES_DIR,
    backup_dir: Path | str | None = None,
    *,
    ports=APP_PORTS,
) -> dict:
    """Thay DB + ảnh hiện tại bằng nội dung bản sao lưu. Trả thông tin để in ra."""
    zip_path, db_path, images_dir = Path(zip_path), Path(db_path), Path(images_dir)
    busy = running_apps(ports)
    if busy:
        raise Refused(
            "YumeAI đang chạy (cổng " + ", ".join(map(str, busy)) + "). "
            "Tắt main.py và app_gradio.py rồi chạy lại lệnh khôi phục."
        )
    stats = inspect_backup(zip_path)

    safety = None
    if db_path.exists():
        safety = create_backup(db_path, images_dir, backup_dir, label="truoc-khoi-phuc")

    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory() as tmp:
        new_db = Path(tmp) / DB_NAME_IN_ZIP
        new_db.write_bytes(zf.read(DB_NAME_IN_ZIP))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (db_path, db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")):
            leftover.unlink(missing_ok=True)
        shutil.copyfile(new_db, db_path)

        images_dir.mkdir(parents=True, exist_ok=True)
        for old in _image_files(images_dir):
            old.unlink()
        restored = 0
        for name in zf.namelist():
            if not name.startswith("images/") or name.endswith("/"):
                continue
            # Chỉ lấy tên file: một entry kiểu "images/../../x" không được ghi ra ngoài.
            filename = Path(name).name
            if not filename or Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            (images_dir / filename).write_bytes(zf.read(name))
            restored += 1

    return {"stats": stats, "images_restored": restored, "safety_backup": safety}


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _describe(stats: dict) -> str:
    return (f"{stats.get('conversations') or 0} hội thoại, {stats.get('messages') or 0} tin nhắn, "
            f"{stats.get('memory_facts') or 0} ký ức, {stats.get('image_files') or 0} ảnh")


def main(argv=None) -> int:
    # cmd của Windows có thể đang ở code page không có chữ Việt; in "?" còn hơn văng lỗi
    # giữa lúc đang khôi phục.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = argparse.ArgumentParser(description="Sao lưu / khôi phục dữ liệu YumeAI (DB + ảnh).")
    parser.add_argument("--danh-sach", action="store_true", help="liệt kê các bản sao lưu")
    parser.add_argument("--khoi-phuc", metavar="FILE", help="khôi phục từ một file zip")
    parser.add_argument("--dong-y", action="store_true", help="khôi phục không hỏi lại")
    args = parser.parse_args(argv)

    try:
        if args.danh_sach:
            rows = list_backups()
            if not rows:
                print(f"Chưa có bản sao lưu nào trong {default_backup_dir()}")
            for row in rows:
                manifest = row["manifest"] or {}
                note = manifest.get("error") or (_describe(manifest) if "conversations" in manifest else "bản cũ, không có manifest")
                print(f"{row['path'].name:48} {_size(row['size']):>9}  {note}")
            return 0

        if args.khoi_phuc:
            stats = inspect_backup(args.khoi_phuc)
            print(f"Bản sao lưu: {Path(args.khoi_phuc).name} — {_describe(stats)}.")
            if not args.dong_y:
                print("Khôi phục sẽ THAY toàn bộ dữ liệu hiện tại (dữ liệu hiện tại được sao lưu trước).")
                if input('Gõ "CO" để tiếp tục: ').strip().upper() not in ("CO", "CÓ"):
                    print("Đã thôi, không đổi gì.")
                    return 2
            result = restore_backup(args.khoi_phuc)
            if result["safety_backup"]:
                print(f"Dữ liệu trước khi khôi phục đã lưu ở: {result['safety_backup']}")
            print(f"Đã khôi phục: {_describe(result['stats'])}.")
            return 0

        target = create_backup()
        with zipfile.ZipFile(target) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        print(f"Đã sao lưu vào: {target} ({_size(target.stat().st_size)})")
        print(f"Gồm {_describe(manifest)}.")
        return 0
    except Refused as e:
        print(f"Không làm: {e}")
        return 2
    except FileNotFoundError as e:
        print(f"Không làm: {e}")
        return 2
    except Exception as e:  # noqa: BLE001 — in lỗi dễ hiểu thay vì traceback dài
        print(f"Lỗi: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
