"""Đóng khung thông điệp JSON và kiểm tra danh sách file."""

import json
import socket
import struct
from pathlib import PureWindowsPath

from .config import PROTOCOL

Manifest = list[tuple[str, int]]


def read_exact(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise OSError("Mất kết nối trước khi nhận đủ dữ liệu.")
        data.extend(chunk)
    return bytes(data)


def send_message(conn: socket.socket, message: dict[str, object]) -> None:
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > 65536:
        raise ValueError("Danh sách file quá dài.")
    conn.sendall(struct.pack("!I", len(data)) + data)


def read_message(conn: socket.socket) -> dict[str, object]:
    size = struct.unpack("!I", read_exact(conn, 4))[0]
    if not 1 <= size <= 65536:
        raise ValueError("Độ dài thông điệp không hợp lệ.")
    value = json.loads(read_exact(conn, size))
    if not isinstance(value, dict):
        raise ValueError("Thông điệp phải là một đối tượng JSON.")
    return value


def valid_name(name: object) -> str:
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240:
        raise ValueError("Tên file rỗng hoặc quá dài (tối đa 240 byte UTF-8).")
    if (name.endswith((".", " ")) or PureWindowsPath(name).is_reserved()
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)):
        raise ValueError("Tên file không hợp lệ; không hỗ trợ đường dẫn thư mục.")
    return name


def parse_manifest(message: dict[str, object]) -> Manifest:
    entries = message.get("files")
    if message.get("protocol") != PROTOCOL or not isinstance(entries, list):
        raise ValueError("Yêu cầu không đúng giao thức Relay LAN.")
    if not 1 <= len(entries) <= 1000:
        raise ValueError("Mỗi lượt phải có từ 1 đến 1000 file.")
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Thông tin file không hợp lệ.")
        name, size = valid_name(entry.get("name")), entry.get("size")
        if type(size) is not int or not 0 <= size < 2**63:
            raise ValueError("Kích thước file không hợp lệ.")
        result.append((name, size))
    return result
