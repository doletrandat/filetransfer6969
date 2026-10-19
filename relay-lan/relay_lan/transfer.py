"""Gửi, nhận và lưu từng file sau khi máy nhận đồng ý."""

import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from . import config
from .config import CHUNK, PROTOCOL
from .console import Progress
from .protocol import parse_manifest, read_exact, read_message, send_message, valid_name


def create_output(directory: Path, name: str) -> tuple[Path, BinaryIO]:
    original = Path(name)
    for number in range(10000):
        filename = name if number == 0 else f"{original.stem} ({number}){original.suffix}"
        target = directory / filename
        try:
            # Mở xb trực tiếp để tránh khoảng đua giữa kiểm tra tồn tại và tạo file.
            return target, target.open("xb")  # Tạo độc quyền, không ghi đè.
        except FileExistsError:
            continue
    raise OSError("Có quá nhiều file trùng tên.")


def receive_batch(conn: socket.socket, directory: Path, host: str,
                  ask: Callable[[str], str] = input) -> None:
    message = read_message(conn)
    files = parse_manifest(message)
    sender = str(message.get("sender", "Không rõ"))[:80]
    print(f"\n{sender!r} ({host}) muốn gửi {len(files)} file, "
          f"tổng {sum(size for _, size in files)} byte:", flush=True)
    for name, size in files:
        print(f"  {name!r}: {size} byte", flush=True)
    # Chỉ y mới chấp nhận; chưa tạo file hoặc đọc nội dung file trước quyết định này.
    if ask("Đồng ý nhận? [y/N]: ").strip().lower() != "y":
        send_message(conn, {"accepted": False})
        print("Đã từ chối.", flush=True)
        return
    send_message(conn, {"accepted": True})
    for index, (name, size) in enumerate(files, 1):
        target, output = create_output(directory, name)
        progress = Progress(f"Nhận [{index}/{len(files)}] {name!r}", size)
        try:
            with output:
                remaining = size
                progress.update(0)
                while remaining:
                    # Không đọc quá số byte đã khai báo để giữ đúng ranh giới các file.
                    chunk = read_exact(conn, min(CHUNK, remaining))
                    output.write(chunk)
                    remaining -= len(chunk)
                    progress.update(len(chunk))
        except (OSError, ValueError, KeyboardInterrupt):
            target.unlink(missing_ok=True)  # Chỉ xóa file của lượt đang nhận dở.
            raise
        # Xác nhận sau khi file đã ghi và đóng thành công; trả tên thực tế nếu bị trùng.
        send_message(conn, {"saved": target.name})
        print(f"Đã lưu: {target}", flush=True)


def send_files(host: str, port: int, paths: list[Path]) -> None:
    # Lập danh sách tên/kích thước trước khi kết nối để báo lỗi nguồn ngay tại máy gửi.
    files: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Không phải file thường: {path}. Không hỗ trợ thư mục.")
        files.append({"name": valid_name(path.name), "size": path.stat().st_size})
    offer: dict[str, object] = {"protocol": PROTOCOL, "sender": socket.gethostname(),
                               "files": files}
    manifest = parse_manifest(offer)
    with socket.create_connection((host, port), timeout=10) as conn:
        conn.settimeout(config.CONSENT_TIMEOUT + 5)  # Chờ máy nhận xác nhận hoặc hết giờ.
        send_message(conn, offer)
        print("Đang chờ máy nhận đồng ý...", flush=True)
        if read_message(conn).get("accepted") is not True:
            raise OSError("Máy nhận đã từ chối.")
        # Sau khi được đồng ý, chuyển từ thời hạn xác nhận sang timeout truyền dữ liệu.
        conn.settimeout(config.TIMEOUT)
        for index, (path, (name, size)) in enumerate(zip(paths, manifest, strict=True), 1):
            progress = Progress(f"Gửi [{index}/{len(paths)}] {name!r}", size)
            with path.open("rb") as source:
                # File có thể đổi trong lúc chờ đồng ý; đối chiếu lại kích thước đã báo.
                if os.fstat(source.fileno()).st_size != size:
                    raise OSError(f"Kích thước file đã thay đổi: {path}")
                remaining = size
                progress.update(0)
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise OSError(f"File nguồn bị thu ngắn: {path}")
                    conn.sendall(chunk)
                    remaining -= len(chunk)
                    progress.update(len(chunk))
            # Chờ xác nhận lưu từng file rồi mới gửi tiếp hoặc báo hoàn tất cả lượt.
            saved = read_message(conn).get("saved")
            if not isinstance(saved, str):
                raise OSError("Máy nhận chưa xác nhận lưu file.")
            print(f"Máy nhận đã lưu: {saved!r}", flush=True)
    print(f"Hoàn tất gửi {len(paths)} file.", flush=True)
