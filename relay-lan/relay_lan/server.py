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
    # Dùng chung một bộ đọc bàn phím cho mọi lượt nhận, kể cả sau khi hết giờ chờ.
    ask = Confirmation()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((host, port))
        server.listen(5)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener = None
        try:
            try:
                udp.bind((host, udp_port))
                # UDP chạy riêng để máy vẫn được tìm thấy khi đang hỏi hoặc nhận file.
                listener = threading.Thread(target=discovery_responder,
                                            args=(udp, server.getsockname()[1], name, stop),
                                            daemon=True)
                listener.start()
                print(f"Tìm máy qua UDP: {udp.getsockname()[1]}", flush=True)
            except OSError as error:
                # Lỗi mở cổng tìm máy không ngăn việc nhận TCP qua IP thủ công.
                print(f"Không bật được tìm máy: {error}. Vẫn nhận qua IP thủ công.", flush=True)
            print(f"Sẵn sàng nhận TCP {host}:{server.getsockname()[1]} | "
                  f"Thư mục: {directory.resolve()}", flush=True)
            while True:
                # Xử lý tuần tự để các câu hỏi xác nhận không chen vào nhau.
                conn, address = server.accept()
                with conn:
                    conn.settimeout(config.TIMEOUT)
                    try:
                        receive_batch(conn, directory, address[0], ask)
                    except (OSError, ValueError, EOFError, RecursionError) as error:
                        # Lỗi của một lượt không kết thúc vòng lặp chờ máy gửi tiếp theo.
                        print(f"\nKhông hoàn tất lượt nhận: {error}", flush=True)
        finally:
            # Báo luồng UDP dừng, chờ nó thoát rồi mới đóng socket dùng chung.
            stop.set()
            if listener:
                listener.join(timeout=1)
            udp.close()
