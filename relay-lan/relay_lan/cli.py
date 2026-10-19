"""Phân tích lệnh, chọn máy nhận và điều phối chương trình."""

import argparse
import socket
import sys
from io import TextIOWrapper
from pathlib import Path

from .config import TCP_PORT, UDP_PORT
from .discovery import Peer, discover
from .server import receive
from .transfer import send_files


def show_peers(peers: list[Peer]) -> None:
    for index, (name, host, port) in enumerate(peers, 1):
        print(f"{index}. {name!r} — {host}:{port}")
    if not peers:
        print("Không thấy máy nhận. Kiểm tra cùng LAN/Firewall hoặc dùng --host IP.")


def port_number(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Cổng phải nằm trong khoảng 1–65535.")
    return number


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Relay LAN: tìm máy và gửi nhiều file.")
    commands = parser.add_subparsers(dest="mode", required=True)
    receiver = commands.add_parser("receive", help="Chờ nhận và xuất hiện trong danh sách máy")
    receiver.add_argument("--host", default="0.0.0.0")
    receiver.add_argument("--name", default=socket.gethostname(), help="Tên hiển thị của máy")
    receiver.add_argument("--directory", type=Path, default=Path("received"))
    sender = commands.add_parser("send", help="Tìm/chọn máy rồi gửi một hoặc nhiều file")
    sender.add_argument("files", nargs="+", type=Path)
    sender.add_argument("--host", help="Nhập IP thủ công, bỏ qua bước tìm máy")
    scanner = commands.add_parser("discover", help="Liệt kê máy nhận trong LAN")
    for command in (receiver, sender, scanner):
        command.add_argument("--discovery-port", type=port_number, default=UDP_PORT)
    for command in (receiver, sender):
        command.add_argument("--port", type=port_number, default=TCP_PORT)
    args = parser.parse_args()
    try:
        if args.mode == "receive":
            receive(args.host, args.port, args.discovery_port, args.directory, args.name)
        elif args.mode == "discover":
            show_peers(discover(args.discovery_port))
        else:
            host, port = args.host, args.port
            if not host:
                peers = discover(args.discovery_port)
                show_peers(peers)
                if not peers:
                    parser.exit(1)
                choice = int(input("Chọn số máy nhận: "))
                if not 1 <= choice <= len(peers):
                    raise ValueError("Số máy được chọn không hợp lệ.")
                _, host, port = peers[choice - 1]
            send_files(host, port, args.files)
    except KeyboardInterrupt:
        parser.exit(130, "\nĐã hủy.\n")
    except (OSError, ValueError, EOFError, RecursionError) as error:
        parser.exit(1, f"Lỗi: {error}\n")
