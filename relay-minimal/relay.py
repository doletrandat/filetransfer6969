"""Gửi/nhận từng file qua TCP trong mạng LAN, chỉ dùng thư viện chuẩn."""

import argparse
import os
import socket
import struct
import sys
from io import TextIOWrapper
from pathlib import Path, PureWindowsPath

CHUNK_SIZE = 1024 * 1024  # Đọc từng khối 1 MiB, không nạp cả file vào RAM.
HEADER = struct.Struct("!HQ")  # 2 byte độ dài tên UTF-8 + 8 byte kích thước file.


def read_exact(connection: socket.socket, size: int) -> bytes:
    """TCP có thể chia nhỏ dữ liệu: đọc đủ số byte yêu cầu mới trả về."""
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("Kết nối bị đóng trước khi nhận đủ dữ liệu.")
        data.extend(chunk)
    return bytes(data)


def send_file(host: str, port: int, source: Path) -> None:
    name = source.name.encode("utf-8")
    if not 1 <= len(name) <= 1024:
        raise ValueError("Tên file quá dài hoặc rỗng.")
    with source.open("rb") as file, socket.create_connection((host, port), timeout=10) as conn:
        conn.settimeout(60)
        size = os.fstat(file.fileno()).st_size
        conn.sendall(HEADER.pack(len(name), size) + name)
        remaining = size
        while remaining:
            chunk = file.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError("File nguồn bị thu ngắn trong lúc gửi.")
            conn.sendall(chunk)
            remaining -= len(chunk)
        # Chỉ báo thành công khi máy nhận đã lưu đủ file và xác nhận.
        if read_exact(conn, 2) != b"OK":
            raise OSError("Máy nhận chưa xác nhận lưu file.")
    print(f"Đã gửi: {source.name} ({size} byte)")


def receive_file(conn: socket.socket, directory: Path) -> Path:
    name_size, size = HEADER.unpack(read_exact(conn, HEADER.size))
    if not 1 <= name_size <= 1024:
        raise ValueError("Độ dài tên file không hợp lệ.")
    name = read_exact(conn, name_size).decode("utf-8")
    if (name.endswith((".", " ")) or PureWindowsPath(name).is_reserved()
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)):
        raise ValueError("Tên file không hợp lệ; chỉ nhận tên, không nhận đường dẫn.")
    target = directory / name
    output = target.open("xb")  # Không ghi đè file đã tồn tại.
    try:
        with output:
            remaining = size
            while remaining:
                chunk = read_exact(conn, min(CHUNK_SIZE, remaining))
                output.write(chunk)
                remaining -= len(chunk)
    except (OSError, ValueError, KeyboardInterrupt):
        target.unlink(missing_ok=True)  # Xóa phần file nhận dở nếu bị ngắt.
        raise
    conn.sendall(b"OK")
    return target


def receive(host: str, port: int, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((host, port))
        server.listen(1)
        print(
            f"Đang chờ tại {host}:{server.getsockname()[1]} | Lưu: {directory.resolve()}",
            flush=True,
        )
        while True:
            conn, address = server.accept()
            with conn:
                conn.settimeout(60)
                try:
                    target = receive_file(conn, directory)
                    print(f"Đã nhận từ {address[0]}: {target}", flush=True)
                except (OSError, ValueError) as error:
                    print(f"Không nhận được file: {error}", flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Relay: gửi một file qua mạng LAN.")
    commands = parser.add_subparsers(dest="mode", required=True)
    receiver = commands.add_parser("receive", help="Chờ nhận file")
    receiver.add_argument("--host", default="0.0.0.0")
    receiver.add_argument("--directory", type=Path, default=Path("received"))
    sender = commands.add_parser("send", help="Gửi một file tới IP máy nhận")
    sender.add_argument("host", help="Địa chỉ IP máy nhận")
    sender.add_argument("file", type=Path)
    for command in (receiver, sender):
        command.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Cổng phải nằm trong khoảng 1–65535.")
    try:
        if args.mode == "receive":
            receive(args.host, args.port, args.directory)
        else:
            send_file(args.host, args.port, args.file)
    except KeyboardInterrupt:
        print("\nĐã dừng.")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Lỗi: {error}\n")


if __name__ == "__main__":
    main()
