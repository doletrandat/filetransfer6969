"""Truyền TCP thật giữa tiến trình gửi/nhận, không cần thư viện chạy ứng dụng."""

import os
import queue
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def receiver(tmp_path: Path) -> Iterator[tuple[int, Path]]:
    destination = tmp_path / "received"
    # Cổng 0 để hệ điều hành chọn cổng rảnh cho từng lần kiểm tra.
    script = (
        "from pathlib import Path; from relay import receive; "
        "import sys; receive('127.0.0.1', 0, Path(sys.argv[1]))"
    )
    with subprocess.Popen(
        [sys.executable, "-S", "-u", "-c", script, str(destination)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    ) as process:
        output = process.stdout
        assert output is not None
        lines: queue.Queue[str] = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(output.readline()), daemon=True)
        reader.start()
        try:
            ready = lines.get(timeout=10)
            assert "127.0.0.1:" in ready, ready
            port = int(ready.split("127.0.0.1:")[1].split()[0])
            yield port, destination
        finally:
            process.terminate()
            process.wait(timeout=10)
            reader.join(timeout=2)


def send(port: int, source: Path) -> subprocess.CompletedProcess[str]:
    # -S tắt site-packages: chứng minh đường chạy mới không cần framework.
    return subprocess.run(
        [sys.executable, "-S", "-m", "relay", "send", "127.0.0.1", str(source),
         "--port", str(port)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


@pytest.mark.parametrize("size", [0, 123, 3 * 1024 * 1024 + 17])
def test_transfer(receiver: tuple[int, Path], tmp_path: Path, size: int) -> None:
    port, destination = receiver
    source = tmp_path / "báo cáo tiếng Việt.bin"
    content = os.urandom(size)
    source.write_bytes(content)
    result = send(port, source)
    assert result.returncode == 0, result.stderr
    assert "Đã gửi" in result.stdout
    assert (destination / source.name).read_bytes() == content


def test_duplicate_keeps_original(receiver: tuple[int, Path], tmp_path: Path) -> None:
    port, destination = receiver
    source = tmp_path / "same.txt"
    source.write_bytes(b"first")
    assert send(port, source).returncode == 0
    source.write_bytes(b"replacement")
    result = send(port, source)
    assert result.returncode != 0
    assert "Đã gửi" not in result.stdout
    assert (destination / source.name).read_bytes() == b"first"


def test_fragmented_header_and_body(receiver: tuple[int, Path]) -> None:
    port, destination = receiver
    name, content = b"fragmented.bin", bytes(range(256)) * 31
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        for byte in struct.pack("!HQ", len(name), len(content)) + name:
            conn.sendall(bytes([byte]))
        for index in range(0, len(content), 37):
            conn.sendall(content[index:index + 37])
        assert conn.makefile("rb").read(2) == b"OK"
    assert (destination / name.decode()).read_bytes() == content


@pytest.mark.parametrize("name", [
    b"../escape", b"..\\escape", b"C:escape", b".", b"\xff",
    b"NUL", b"COM1.txt", b"trailing.", b"space ",
])
def test_bad_name_rejected(receiver: tuple[int, Path], name: bytes) -> None:
    port, destination = receiver
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(struct.pack("!HQ", len(name), 0) + name)
        assert conn.recv(2) == b""
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("name_size", [0, 1025])
def test_bad_name_length(receiver: tuple[int, Path], name_size: int) -> None:
    port, destination = receiver
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(struct.pack("!HQ", name_size, 0))
        assert conn.recv(2) == b""
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("stage", ["header", "name", "body"])
def test_disconnect_cleans_up_and_accepts_next_file(
    receiver: tuple[int, Path], tmp_path: Path, stage: str,
) -> None:
    port, destination = receiver
    packets = {
        "header": b"\x00",
        "name": struct.pack("!HQ", 20, 100) + b"partial",
        "body": struct.pack("!HQ", 11, 100) + b"partial.bin" + b"short",
    }
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(packets[stage])
        conn.shutdown(socket.SHUT_WR)
        assert conn.recv(2) == b""
    assert list(destination.iterdir()) == []
    source = tmp_path / "next.bin"
    source.write_bytes(b"next transfer")
    assert send(port, source).returncode == 0
    assert (destination / source.name).read_bytes() == b"next transfer"


def test_missing_source(receiver: tuple[int, Path], tmp_path: Path) -> None:
    port, destination = receiver
    result = send(port, tmp_path / "missing.bin")
    assert result.returncode != 0
    assert "Đã gửi" not in result.stdout
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("arguments, status", [
    (["--help"], 0), (["receive", "--port", "0"], 2),
    (["receive", "--port", "65536"], 2),
])
def test_cli_with_windows_pipe_encoding(arguments: list[str], status: int) -> None:
    result = subprocess.run(
        [sys.executable, "-S", "-m", "relay", *arguments], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=10,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert result.returncode == status, result.stderr
    assert "Traceback" not in result.stderr
