"""Kiểm tra UDP/TCP thật và các quyết định trên máy nhận bằng tiến trình riêng."""

import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from relay import PROTOCOL, discover, read_message, send_message

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


@dataclass
class Receiver:
    process: subprocess.Popen[str]
    lines: queue.Queue[str]
    directory: Path
    tcp: int = 0
    udp: int = 0

    def wait_for(self, text: str) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            if text in line:
                return line
        raise AssertionError(f"Không thấy thông báo: {text}")

    def answer(self, value: str) -> None:
        self.wait_for("Đồng ý nhận?")
        assert self.process.stdin is not None
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def connect(self) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.tcp), timeout=5)


@pytest.fixture
def receiver(tmp_path: Path) -> Iterator[Receiver]:
    script = (
        "import relay, sys; from pathlib import Path; "
        "relay.TIMEOUT = 2; relay.CONSENT_TIMEOUT = 2; "
        "relay.receive('127.0.0.1', 0, 0, Path(sys.argv[1]), 'Máy thử')"
    )
    process = subprocess.Popen(
        [sys.executable, "-S", "-u", "-c", script, str(tmp_path / "received")],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", env=ENV,
    )
    output = process.stdout
    assert output is not None
    lines: queue.Queue[str] = queue.Queue()

    def collect() -> None:
        for line in output:
            lines.put(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    server = Receiver(process, lines, tmp_path / "received")
    try:
        server.udp = int(server.wait_for("Tìm máy qua UDP:").split(":")[-1])
        ready = server.wait_for("Sẵn sàng nhận TCP")
        server.tcp = int(ready.split("127.0.0.1:")[1].split()[0])
        yield server
    finally:
        process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=3)
        output.close()
        assert process.stdin is not None
        process.stdin.close()


def offer(conn: socket.socket, entries: list[tuple[str, int]]) -> None:
    send_message(conn, {"protocol": PROTOCOL, "sender": "Máy gửi",
                        "files": [{"name": name, "size": size} for name, size in entries]})


def run_sender(receiver: Receiver, paths: list[Path], answer: str = "y",
               automatic: bool = False) -> tuple[int, str]:
    command = [sys.executable, "-S", "relay.py"]
    destination = ["--host", "127.0.0.1", "--port", str(receiver.tcp)]
    if automatic:
        # Giữ toàn bộ bước tìm/chọn thật, chỉ đổi broadcast thành UDP loopback cho CI.
        script = (
            "import relay; original = relay.discover; "
            "relay.discover = lambda port: original(port, seconds=.2, target='127.0.0.1'); "
            "relay.main()"
        )
        command = [sys.executable, "-S", "-c", script]
        destination = ["--discovery-port", str(receiver.udp)]
    with subprocess.Popen(
        [*command, "send", *map(str, paths), *destination],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", env=ENV,
    ) as sender:
        try:
            if automatic:
                assert sender.stdin is not None
                sender.stdin.write("1\n")
                sender.stdin.flush()
            receiver.answer(answer)
            output, _ = sender.communicate(timeout=10)
            return sender.returncode, output
        finally:
            if sender.poll() is None:
                sender.kill()
                sender.wait(timeout=5)


def test_multiple_files_unicode_empty_and_multichunk(receiver: Receiver, tmp_path: Path) -> None:
    paths = []
    for name, content in [("rỗng.txt", b""), ("ảnh.bin", os.urandom(3 * 1024 * 1024 + 19)),
                          ("ghi chú.txt", "Xin chào".encode())]:
        path = tmp_path / name
        path.write_bytes(content)
        paths.append(path)
    status, output = run_sender(receiver, paths)
    assert status == 0, output
    assert "Hoàn tất gửi 3 file" in output
    assert "[3/3]" in output and "100.00%" in output
    for path in paths:
        assert (receiver.directory / path.name).read_bytes() == path.read_bytes()


def test_reject_no_files_created(receiver: Receiver, tmp_path: Path) -> None:
    source = tmp_path / "reject.txt"
    source.write_bytes(b"must not be transferred")
    status, output = run_sender(receiver, [source], "n")
    assert status != 0 and "từ chối" in output
    assert not list(receiver.directory.iterdir())
    assert run_sender(receiver, [source])[0] == 0


def test_no_data_written_before_consent(receiver: Receiver) -> None:
    with receiver.connect() as conn:
        offer(conn, [("wait.bin", 3)])
        receiver.wait_for("Đồng ý nhận?")
        conn.settimeout(0.1)
        with pytest.raises(TimeoutError):
            conn.recv(1)
        assert not list(receiver.directory.iterdir())
        assert receiver.process.stdin is not None
        receiver.process.stdin.write("y\n")
        receiver.process.stdin.flush()
        conn.settimeout(5)
        assert read_message(conn)["accepted"] is True
        conn.sendall(b"abc")
        assert read_message(conn)["saved"] == "wait.bin"
    assert (receiver.directory / "wait.bin").read_bytes() == b"abc"


def test_duplicate_renamed(receiver: Receiver, tmp_path: Path) -> None:
    source = tmp_path / "same.txt"
    source.write_bytes(b"original")
    assert run_sender(receiver, [source])[0] == 0
    source.write_bytes(b"new")
    assert run_sender(receiver, [source, source])[0] == 0
    assert (receiver.directory / "same.txt").read_bytes() == b"original"
    assert (receiver.directory / "same (1).txt").read_bytes() == b"new"
    assert (receiver.directory / "same (2).txt").read_bytes() == b"new"


@pytest.mark.parametrize("mode", ["disconnect", "timeout"])
def test_partial_removed_completed_kept(receiver: Receiver, mode: str) -> None:
    with receiver.connect() as conn:
        offer(conn, [("complete.txt", 2), ("partial.bin", 100)])
        receiver.answer("y")
        assert read_message(conn)["accepted"] is True
        conn.sendall(b"OK")
        assert read_message(conn)["saved"] == "complete.txt"
        conn.sendall(b"short")
        if mode == "disconnect":
            conn.shutdown(socket.SHUT_WR)
        assert conn.recv(1) == b""
    assert (receiver.directory / "complete.txt").read_bytes() == b"OK"
    assert not (receiver.directory / "partial.bin").exists()


def test_consent_timeout_recovers(receiver: Receiver, tmp_path: Path) -> None:
    with receiver.connect() as conn:
        offer(conn, [("expired.txt", 0)])
        assert conn.recv(1) == b""
    assert not list(receiver.directory.iterdir())
    receiver.wait_for("Hết thời gian chờ xác nhận")
    source = tmp_path / "fresh.txt"
    source.write_bytes(b"fresh")
    assert run_sender(receiver, [source])[0] == 0


@pytest.mark.parametrize("name", ["../outside", "..\\outside", "NUL", "COM1.txt", "x.", "", "C:x"])
def test_bad_names_rejected(receiver: Receiver, name: str) -> None:
    with receiver.connect() as conn:
        offer(conn, [(name, 0)])
        assert conn.recv(1) == b""
    assert not list(receiver.directory.iterdir())


@pytest.mark.parametrize("size", [-1, True, "100", 2**63])
def test_invalid_size_rejected(receiver: Receiver, size: object) -> None:
    with receiver.connect() as conn:
        send_message(conn, {"protocol": PROTOCOL, "files": [{"name": "x.txt", "size": size}]})
        assert conn.recv(1) == b""
    assert not list(receiver.directory.iterdir())


@pytest.mark.parametrize("payload", [struct.pack("!I", 65537), struct.pack("!I", 0),
                                   struct.pack("!I", 1) + b"{", struct.pack("!I", 2) + b"[]"])
def test_bad_messages_rejected(receiver: Receiver, payload: bytes) -> None:
    with receiver.connect() as conn:
        conn.sendall(payload)
        assert conn.recv(1) == b""


def test_fragmented_manifest_and_body(receiver: Receiver) -> None:
    message = json.dumps({
        "protocol": PROTOCOL, "files": [{"name": "frag.bin", "size": 3}],
    }).encode()
    with receiver.connect() as conn:
        for byte in struct.pack("!I", len(message)) + message:
            conn.sendall(bytes([byte]))
        receiver.answer("y")
        assert read_message(conn)["accepted"] is True
        for byte in b"abc":
            conn.sendall(bytes([byte]))
        assert read_message(conn)["saved"] == "frag.bin"
    assert (receiver.directory / "frag.bin").read_bytes() == b"abc"


def test_discovery_udp_and_deduplication(receiver: Receiver) -> None:
    peers = discover(receiver.udp, seconds=0.9, target="127.0.0.1")
    assert peers == [("Máy thử", "127.0.0.1", receiver.tcp)]


def test_select_discovered_machine_and_send(receiver: Receiver, tmp_path: Path) -> None:
    source = tmp_path / "selected.txt"
    source.write_bytes(b"send to discovered TCP port")
    status, output = run_sender(receiver, [source], automatic=True)
    assert status == 0, output
    assert "Máy thử" in output and "Chọn số máy nhận" in output
    assert (receiver.directory / source.name).read_bytes() == source.read_bytes()


def test_discovery_remains_active_while_waiting_for_consent(receiver: Receiver) -> None:
    with receiver.connect() as conn:
        offer(conn, [("waiting.txt", 0)])
        receiver.wait_for("Đồng ý nhận?")
        assert discover(receiver.udp, seconds=0.15, target="127.0.0.1")


def test_udp_ignores_noise(receiver: Receiver) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.sendto(b"garbage", ("127.0.0.1", receiver.udp))
        udp.sendto(b"[]", ("127.0.0.1", receiver.udp))
    assert discover(receiver.udp, seconds=0.2, target="127.0.0.1")


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_sender_rejects_nonfiles(tmp_path: Path, kind: str) -> None:
    path = tmp_path if kind == "directory" else tmp_path / "missing"
    result = subprocess.run(
        [sys.executable, "-S", "relay.py", "send", str(path), "--host", "127.0.0.1"],
        cwd=ROOT, capture_output=True, encoding="utf-8", env=ENV, timeout=5,
    )
    assert result.returncode != 0
    assert "Không phải file thường" in result.stderr


@pytest.mark.parametrize("arguments, status", [(["--help"], 0),
                                              (["receive", "--port", "0"], 2)])
def test_cli_windows_encoding(arguments: list[str], status: int) -> None:
    result = subprocess.run(
        [sys.executable, "-S", "relay.py", *arguments], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=5,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert result.returncode == status, result.stderr
    assert "Traceback" not in result.stderr
