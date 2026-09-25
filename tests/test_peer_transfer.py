from __future__ import annotations

import asyncio
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import uvicorn

from relay.app import AppContext, create_app
from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveredDevice, DiscoveryManager
from relay.security import hash_code
from relay.transfers import TransferManager


class FixedDiscovery:
    def __init__(self, device: DiscoveredDevice) -> None:
        self.device = device

    def find_by_code(self, code_hash: str) -> list[DiscoveredDevice]:
        return [self.device] if self.device.code_hash == code_hash else []


async def stream_bytes(value: bytes) -> Any:
    for offset in range(0, len(value), 5):
        yield value[offset : offset + 5]


def open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_server(server: uvicorn.Server) -> None:
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("Test server did not start.")


def test_pinned_tls_pairing_and_transfer(tmp_path: Path) -> None:
    receiver_port = open_port()
    receiver_context = AppContext(tmp_path / "receiver", receiver_port)
    receiver_app = create_app(receiver_context)
    receiver_server = uvicorn.Server(
        uvicorn.Config(
            receiver_app,
            host="127.0.0.1",
            port=receiver_port,
            ssl_certfile=str(receiver_context.data_dir / "identity.crt"),
            ssl_keyfile=str(receiver_context.data_dir / "identity.key"),
            log_level="critical",
        )
    )
    thread = threading.Thread(target=receiver_server.run, daemon=True)
    thread.start()
    try:
        wait_for_server(receiver_server)
        ticket = receiver_context.pairing.create_ticket()
        device = DiscoveredDevice(
            id=receiver_context.identity.id,
            name=receiver_context.identity.name,
            host="127.0.0.1",
            port=receiver_port,
            fingerprint=receiver_context.identity.fingerprint,
            code_hash=hash_code(ticket.code),
            last_seen=time.time(),
        )
        data_dir = tmp_path / "sender"
        manager = TransferManager(
            data_dir,
            SettingsStore(data_dir),
            DeviceIdentity(id="sender", name="Sender", fingerprint="A" * 64),
            cast(DiscoveryManager, FixedDiscovery(device)),
        )
        content = b"end-to-end relay content"

        peer = manager.pair(ticket.code)
        staged = asyncio.run(manager.stage("folder/message.txt", stream_bytes(content)))
        transfer = manager.start_outgoing(peer.id, "folder", [staged.id])
        manager.run_outgoing(transfer.id)
        received = receiver_app.state.context.transfers.incoming_status(transfer.id)

        assert (Path(received.destination) / "folder" / "message.txt").read_bytes() == content
        assert manager.list_outgoing()[0].status == "complete"

        second_content = b"resume after sender restart"
        second_staged = asyncio.run(
            manager.stage("folder/second.txt", stream_bytes(second_content))
        )
        pending = manager.start_outgoing(peer.id, "second batch", [second_staged.id])
        new_ticket = receiver_context.pairing.create_ticket()
        restored = TransferManager(
            data_dir,
            SettingsStore(data_dir),
            DeviceIdentity(id="sender", name="Sender", fingerprint="A" * 64),
            cast(DiscoveryManager, FixedDiscovery(
                replace(device, code_hash=hash_code(new_ticket.code))
            )),
        )
        assert restored.list_outgoing()[0].status == "failed"
        assert restored.list_staged()[0].id == second_staged.id
        restored.pair(new_ticket.code)
        restored.retry_outgoing(pending.id)
        second_received = receiver_app.state.context.transfers.incoming_status(pending.id)
        assert (
            Path(second_received.destination) / "folder" / "second.txt"
        ).read_bytes() == second_content
        assert restored.list_outgoing()[0].status == "complete"
    finally:
        receiver_server.should_exit = True
        thread.join(timeout=10)
