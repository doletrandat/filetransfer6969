"""Chạy máy nhận TCP và quản lý vòng đời luồng tìm máy UDP."""

import socket
import threading
from pathlib import Path

from . import config
from .console import Confirmation
from .discovery import discovery_responder
from .transfer import receive_batch


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
                    conn.settimeout(config.TIMEOUT)
                    try:
                        receive_batch(conn, directory, address[0], ask)
                    except (OSError, ValueError, EOFError, RecursionError) as error:
                        print(f"\nKhông hoàn tất lượt nhận: {error}", flush=True)
        finally:
            stop.set()
            if listener:
                listener.join(timeout=1)
            udp.close()
