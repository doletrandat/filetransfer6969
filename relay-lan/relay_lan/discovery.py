"""Tìm máy nhận và trả lời yêu cầu tìm kiếm qua UDP."""

import json
import secrets
import socket
import threading
import time

from .config import PROTOCOL, UDP_PORT

Peer = tuple[str, str, int]  # Tên máy, IP, cổng TCP.


def discovery_responder(udp: socket.socket, tcp_port: int, name: str,
                        stop: threading.Event) -> None:
    # Thức dậy định kỳ để kiểm tra tín hiệu dừng, kể cả khi không có gói UDP.
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
            # Trả lại nonce và cổng TCP để máy gửi ghép đúng phản hồi với lượt tìm.
            response = {"protocol": PROTOCOL, "nonce": nonce, "name": name[:80],
                        "port": tcp_port}
            udp.sendto(json.dumps(response).encode("utf-8"), address)
        except (OSError, ValueError, RecursionError):
            continue


def discover(udp_port: int = UDP_PORT, seconds: float = 3,
             target: str = "255.255.255.255") -> list[Peer]:
    # Nonce phân biệt từng lượt tìm máy, không dùng để xác thực danh tính thiết bị.
    nonce = secrets.token_hex(8)
    request = json.dumps({"protocol": PROTOCOL, "action": "discover", "nonce": nonce}).encode()
    peers: dict[tuple[str, int], Peer] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Cổng 0 để hệ điều hành chọn cổng tạm nhận phản hồi từ các máy trong LAN.
        udp.bind(("0.0.0.0", 0))
        deadline, next_send = time.monotonic() + seconds, 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                udp.sendto(request, (target, udp_port))
                next_send = now + 0.7  # Gửi lại vì UDP có thể mất gói.
            # Chờ ngắn để còn gửi lại yêu cầu và kiểm tra thời hạn tìm kiếm.
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
                # IP lấy từ gói nhận; khóa IP/cổng loại các phản hồi lặp lại.
                peers[address[0], port] = (name, address[0], port)
            except (OSError, ValueError, RecursionError):
                continue
    return sorted(peers.values(), key=lambda peer: (peer[0].casefold(), peer[1], peer[2]))
