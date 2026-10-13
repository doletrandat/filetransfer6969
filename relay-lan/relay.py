"""Relay LAN: tự tìm máy, xác nhận và truyền nhiều file bằng thư viện chuẩn."""

import argparse
import json
import os
import queue
import secrets
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from io import TextIOWrapper
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

CHUNK = 1024 * 1024
PROTOCOL = "relay-lan-v1"
TCP_PORT, UDP_PORT = 8765, 8766
TIMEOUT = 60
CONSENT_TIMEOUT = 120
Peer = tuple[str, str, int]  # Tên máy, IP, cổng TCP.
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


def create_output(directory: Path, name: str) -> tuple[Path, BinaryIO]:
    original = Path(name)
    for number in range(10000):
        filename = name if number == 0 else f"{original.stem} ({number}){original.suffix}"
        target = directory / filename
        try:
            return target, target.open("xb")  # Tạo độc quyền, không ghi đè.
        except FileExistsError:
            continue
    raise OSError("Có quá nhiều file trùng tên.")


class Progress:
    def __init__(self, label: str, size: int) -> None:
        self.label, self.size, self.done, self.last = label, size, 0, 0.0

    def update(self, count: int) -> None:
        self.done += count
        now = time.monotonic()
        if self.done == self.size or now - self.last >= 0.2:
            percent = self.done * 100 / self.size if self.size else 100
            print(f"\r{self.label}: {percent:6.2f}% ({self.done}/{self.size} byte)",
                  end="\n" if self.done == self.size else "", flush=True)
            self.last = now


class Confirmation:
    """Một luồng đọc bàn phím dùng chung, không kẹt máy nhận khi hết giờ chờ."""

    def __init__(self) -> None:
        self.answers: queue.Queue[str | None] = queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self.read_input, daemon=True).start()

    def read_input(self) -> None:
        for line in sys.stdin:
            self.answers.put(line)
        self.closed.set()
        self.answers.put(None)

    def __call__(self, prompt: str) -> str:
        # Bỏ phím nhập khi chưa có yêu cầu; không dùng lại câu trả lời cũ.
        while not self.answers.empty():
            self.answers.get_nowait()
        if self.closed.is_set():
            raise EOFError("Đầu vào đã đóng; không thể xác nhận nhận file.")
        print(prompt, flush=True)
        try:
            answer = self.answers.get(timeout=CONSENT_TIMEOUT)
        except queue.Empty as error:
            raise TimeoutError("Hết thời gian chờ xác nhận.") from error
        if answer is None:
            raise EOFError("Đầu vào đã đóng.")
        return answer


def receive_batch(conn: socket.socket, directory: Path, host: str,
                  ask: Callable[[str], str] = input) -> None:
    message = read_message(conn)
    files = parse_manifest(message)
    sender = str(message.get("sender", "Không rõ"))[:80]
    print(f"\n{sender!r} ({host}) muốn gửi {len(files)} file, "
          f"tổng {sum(size for _, size in files)} byte:", flush=True)
    for name, size in files:
        print(f"  {name!r}: {size} byte", flush=True)
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
                    chunk = read_exact(conn, min(CHUNK, remaining))
                    output.write(chunk)
                    remaining -= len(chunk)
                    progress.update(len(chunk))
        except (OSError, ValueError, KeyboardInterrupt):
            target.unlink(missing_ok=True)  # Chỉ xóa file của lượt đang nhận dở.
            raise
        send_message(conn, {"saved": target.name})
        print(f"Đã lưu: {target}", flush=True)


def send_files(host: str, port: int, paths: list[Path]) -> None:
    files: list[dict[str, object]] = []
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Không phải file thường: {path}. Không hỗ trợ thư mục.")
        files.append({"name": valid_name(path.name), "size": path.stat().st_size})
    offer: dict[str, object] = {"protocol": PROTOCOL, "sender": socket.gethostname(),
                               "files": files}
    manifest = parse_manifest(offer)
    with socket.create_connection((host, port), timeout=10) as conn:
        conn.settimeout(CONSENT_TIMEOUT + 5)  # Chờ máy nhận xác nhận hoặc hết giờ.
        send_message(conn, offer)
        print("Đang chờ máy nhận đồng ý...", flush=True)
        if read_message(conn).get("accepted") is not True:
            raise OSError("Máy nhận đã từ chối.")
        conn.settimeout(TIMEOUT)
        for index, (path, (name, size)) in enumerate(zip(paths, manifest, strict=True), 1):
            progress = Progress(f"Gửi [{index}/{len(paths)}] {name!r}", size)
            with path.open("rb") as source:
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
            saved = read_message(conn).get("saved")
            if not isinstance(saved, str):
                raise OSError("Máy nhận chưa xác nhận lưu file.")
            print(f"Máy nhận đã lưu: {saved!r}", flush=True)
    print(f"Hoàn tất gửi {len(paths)} file.", flush=True)


def discovery_responder(udp: socket.socket, tcp_port: int, name: str,
                        stop: threading.Event) -> None:
    udp.settimeout(0.5)
    while not stop.is_set():
        try:
            data, address = udp.recvfrom(2048)
            request = json.loads(data)
            if (not isinstance(request, dict) or request.get("protocol") != PROTOCOL
                    or request.get("action") != "discover"):
                continue
            nonce = request.get("nonce")
            if not isinstance(nonce, str) or len(nonce) != 16:
                continue
            response = {"protocol": PROTOCOL, "nonce": nonce, "name": name[:80],
                        "port": tcp_port}
            udp.sendto(json.dumps(response).encode("utf-8"), address)
        except (OSError, ValueError, RecursionError):
            continue


def discover(udp_port: int = UDP_PORT, seconds: float = 3,
             target: str = "255.255.255.255") -> list[Peer]:
    nonce = secrets.token_hex(8)
    request = json.dumps({"protocol": PROTOCOL, "action": "discover", "nonce": nonce}).encode()
    peers: dict[tuple[str, int], Peer] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("0.0.0.0", 0))
        deadline, next_send = time.monotonic() + seconds, 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                udp.sendto(request, (target, udp_port))
                next_send = now + 0.7  # Gửi lại vì UDP có thể mất gói.
            udp.settimeout(max(0.01, min(0.2, deadline - now)))
            try:
                data, address = udp.recvfrom(2048)
                peer = json.loads(data)
                if (not isinstance(peer, dict) or peer.get("protocol") != PROTOCOL
                        or peer.get("nonce") != nonce):
                    continue
                name, port = peer.get("name"), peer.get("port")
                if (not isinstance(name, str) or not 1 <= len(name) <= 80
                        or type(port) is not int or not 1 <= port <= 65535):
                    continue
                peers[address[0], port] = (name, address[0], port)
            except (OSError, ValueError, RecursionError):
                continue
    return sorted(peers.values(), key=lambda peer: (peer[0].casefold(), peer[1], peer[2]))


def show_peers(peers: list[Peer]) -> None:
    for index, (name, host, port) in enumerate(peers, 1):
        print(f"{index}. {name!r} — {host}:{port}")
    if not peers:
        print("Không thấy máy nhận. Kiểm tra cùng LAN/Firewall hoặc dùng --host IP.")


def receive(host: str, port: int, udp_port: int, directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    ask = Confirmation()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((host, port))
        server.listen(5)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener = None
        try:
            try:
                udp.bind((host, udp_port))
                listener = threading.Thread(target=discovery_responder,
                                            args=(udp, server.getsockname()[1], name, stop),
                                            daemon=True)
                listener.start()
                print(f"Tìm máy qua UDP: {udp.getsockname()[1]}", flush=True)
            except OSError as error:
                print(f"Không bật được tìm máy: {error}. Vẫn nhận qua IP thủ công.", flush=True)
            print(f"Sẵn sàng nhận TCP {host}:{server.getsockname()[1]} | "
                  f"Thư mục: {directory.resolve()}", flush=True)
            while True:
                conn, address = server.accept()
                with conn:
                    conn.settimeout(TIMEOUT)
                    try:
                        receive_batch(conn, directory, address[0], ask)
                    except (OSError, ValueError, EOFError, RecursionError) as error:
                        print(f"\nKhông hoàn tất lượt nhận: {error}", flush=True)
        finally:
            stop.set()
            if listener:
                listener.join(timeout=1)
            udp.close()


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


if __name__ == "__main__":
    main()
