from __future__ import annotations

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn

from relay.app import AppContext, create_app
from relay.config import DeviceIdentity, SettingsStore, load_or_create_identity
from relay.discovery import DiscoveredDevice, DiscoveryManager
from relay.network import PeerConnectionError
from relay.transfers import TransferManager


class FixedDiscovery:
    def __init__(self, device: DiscoveredDevice) -> None:
        self.device = device

    def list_devices(self) -> list[DiscoveredDevice]:
        return [self.device]


async def stream_bytes(value: bytes) -> Any:
    yield value


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


def sender(tmp_path: Path, device: DiscoveredDevice, index: int = 0) -> TransferManager:
    directory = tmp_path / f"sender-{index}"
    return TransferManager(
        directory, SettingsStore(directory),
        DeviceIdentity(id=f"sender-{index}", name=f"Sender {index}", fingerprint="A" * 64),
        cast(DiscoveryManager, FixedDiscovery(device)),
    )


@pytest.mark.parametrize("changed_ip", [False, True])
def test_pinned_tls_consent_transfer_and_restart(tmp_path: Path, changed_ip: bool) -> None:
    port = open_port()
    if changed_ip:
        directory = tmp_path / "receiver"
        directory.mkdir()
        load_or_create_identity(directory, "Receiver", ["192.168.1.10"], port)
    receiver = AppContext(tmp_path / "receiver", port)
    receiver.settings.update(str(tmp_path / "received"))
    server = uvicorn.Server(uvicorn.Config(
        create_app(receiver), host="127.0.0.1", port=port,
        ssl_certfile=str(receiver.data_dir / "identity.crt"),
        ssl_keyfile=str(receiver.data_dir / "identity.key"), log_level="critical",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for_server(server)
        device = DiscoveredDevice(
            id=receiver.identity.id, name=receiver.identity.name, host="127.0.0.1", port=port,
            fingerprint=receiver.identity.fingerprint, code_hash="", last_seen=time.time(),
        )
        manager = sender(tmp_path, device)
        content = b"consent before sending" * 100000
        staged = asyncio.run(manager.stage("folder/message.txt", stream_bytes(content)))
        empty = asyncio.run(manager.stage("empty.txt", stream_bytes(b"")))
        transfer = manager.start_outgoing(device.id, "two files", [staged.id, empty.id])
        assert transfer.status == "waiting" and transfer.sent_bytes == 0
        assert receiver.transfers.list_incoming() == []
        assert not list((tmp_path / "received").rglob("*.txt"))
        with ThreadPoolExecutor() as pool:
            job = pool.submit(manager.run_outgoing, transfer.id)
            time.sleep(0.4)
            assert transfer.status == "waiting" and not job.done()
            receiver.transfers.decide_offer(transfer.id, True)
            job.result(timeout=15)
        received = receiver.transfers.incoming_status(transfer.id)
        assert transfer.status == received.status == "complete"
        assert transfer.sent_bytes == received.received_bytes == len(content)
        assert received.items[0].target.read_bytes() == content
        assert received.items[1].target.read_bytes() == b""

        # Each subsequent batch needs its own decision; restart cannot reuse consent.
        staged = asyncio.run(manager.stage("restart.txt", stream_bytes(b"restart")))
        interrupted = manager.start_outgoing(device.id, "restart", [staged.id])
        restored = sender(tmp_path, device)
        assert restored.list_outgoing()[0].status == "failed"
        retry = restored.retry_outgoing(interrupted.id)
        assert retry.id != interrupted.id and retry.status == "waiting"
        receiver.transfers.decide_offer(retry.id, True)
        restored.run_outgoing(retry.id)
        assert retry.status == "complete"
        manager.cancel_outgoing(interrupted.id)
        assert receiver.transfers.verify_offer(
            interrupted.id, manager._credentials[interrupted.id].session_token,
        ).status == "cancelled"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_concurrent_senders_accept_reject_expire_and_cancel(tmp_path: Path) -> None:
    port = open_port()
    receiver = AppContext(tmp_path / "receiver", port)
    server = uvicorn.Server(uvicorn.Config(
        create_app(receiver), host="127.0.0.1", port=port,
        ssl_certfile=str(receiver.data_dir / "identity.crt"),
        ssl_keyfile=str(receiver.data_dir / "identity.key"), log_level="critical",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for_server(server)
        device = DiscoveredDevice(
            receiver.identity.id, receiver.identity.name, "127.0.0.1", port,
            receiver.identity.fingerprint, "", time.time(),
        )
        managers = [sender(tmp_path, device, i) for i in range(4)]
        transfers = []
        for manager in managers:
            staged = asyncio.run(manager.stage("file.txt", stream_bytes(b"payload")))
            transfers.append(manager.start_outgoing(device.id, "batch", [staged.id]))
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(m.run_outgoing, t.id)
                    for m, t in zip(managers, transfers, strict=True)]
            receiver.transfers.decide_offer(transfers[0].id, True)
            receiver.transfers.decide_offer(transfers[1].id, False)
            receiver.transfers._offers[transfers[2].id].expires_at = 0
            managers[3].cancel_outgoing(transfers[3].id)
            for job in jobs:
                job.result(timeout=15)
        assert [t.status for t in transfers] == ["complete", "rejected", "expired", "cancelled"]
        assert len(receiver.transfers.list_incoming()) == 1
        assert all(m.list_staged() for m in managers[1:])
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_discovered_certificate_mismatch_blocks_request(tmp_path: Path) -> None:
    port = open_port()
    receiver = AppContext(tmp_path / "receiver", port)
    server = uvicorn.Server(uvicorn.Config(
        create_app(receiver), host="127.0.0.1", port=port,
        ssl_certfile=str(receiver.data_dir / "identity.crt"),
        ssl_keyfile=str(receiver.data_dir / "identity.key"), log_level="critical",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for_server(server)
        device = DiscoveredDevice(
            receiver.identity.id, receiver.identity.name, "127.0.0.1", port,
            receiver.identity.fingerprint, "", time.time(),
        )
        manager = sender(tmp_path, replace(device, fingerprint="00:" * 31 + "00"))
        staged = asyncio.run(manager.stage("file.txt", stream_bytes(b"payload")))
        with pytest.raises(PeerConnectionError, match="fingerprint did not match"):
            manager.start_outgoing(device.id, "batch", [staged.id])
        assert receiver.transfers.list_offers() == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
