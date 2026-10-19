"""Hiển thị tiến độ và đọc xác nhận có thời hạn từ bàn phím."""

import queue
import sys
import threading
import time

from . import config


class Progress:
    def __init__(self, label: str, size: int) -> None:
        self.label, self.size, self.done, self.last = label, size, 0, 0.0

    def update(self, count: int) -> None:
        self.done += count
        # Đồng hồ monotonic không bị ảnh hưởng khi người dùng chỉnh giờ hệ thống.
        now = time.monotonic()
        # Hạn chế in liên tục; luôn in lần cuối và coi file rỗng là hoàn tất 100%.
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
        # Đọc stdin ở luồng nền để luồng nhận file có thể hết giờ chờ xác nhận.
        threading.Thread(target=self.read_input, daemon=True).start()

    def read_input(self) -> None:
        for line in sys.stdin:
            self.answers.put(line)
        self.closed.set()
        # None đánh thức bên đang chờ Queue.get khi stdin đã đóng.
        self.answers.put(None)

    def __call__(self, prompt: str) -> str:
        # Bỏ phím nhập khi chưa có yêu cầu; không dùng lại câu trả lời cũ.
        while not self.answers.empty():
            self.answers.get_nowait()
        if self.closed.is_set():
            raise EOFError("Đầu vào đã đóng; không thể xác nhận nhận file.")
        print(prompt, flush=True)
        try:
            # Timeout chỉ kết thúc lượt chờ này; luồng đọc bàn phím vẫn dùng lại.
            answer = self.answers.get(timeout=config.CONSENT_TIMEOUT)
        except queue.Empty as error:
            raise TimeoutError("Hết thời gian chờ xác nhận.") from error
        if answer is None:
            raise EOFError("Đầu vào đã đóng.")
        return answer
