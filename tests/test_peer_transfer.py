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


@pytest.mark.parametrize("changed_ip", [False, True])
def test_pinned_tls_pairing_and_transfer(tmp_path: Path, changed_ip: bool) -> None:
    receiver_port = open_port()
    if changed_ip:
        receiver_dir = tmp_path / "receiver"
        receiver_dir.mkdir()
        load_or_create_identity(receiver_dir, "Receiver", ["192.168.1.10"], receiver_port)
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
        reconnected = restored.pair(new_ticket.code)
        restored.retry_outgoing(pending.id)
        second_received = receiver_app.state.context.transfers.incoming_status(pending.id)
        assert (
            Path(second_received.destination) / "folder" / "second.txt"
        ).read_bytes() == second_content
        assert restored.list_outgoing()[0].status == "complete"

        assert restored.disconnect_peer(peer.id) is True
        assert restored.list_peers() == []
        assert receiver_context.sessions.verify(peer.session_token) is None
        assert receiver_context.sessions.verify(reconnected.session_token) is None
        assert restored.disconnect_peer(peer.id) is False
    finally:
        receiver_server.should_exit = True
        thread.join(timeout=10)


def test_pairing_reports_stale_peer_certificate_without_redeeming_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("relay.app.get_local_addresses", lambda: ["192.168.1.10"])
    port = open_port()
    context = AppContext(tmp_path / "receiver", port)
    server = uvicorn.Server(uvicorn.Config(
        create_app(context), host="127.0.0.1", port=port,
        ssl_certfile=str(context.data_dir / "identity.crt"),
        ssl_keyfile=str(context.data_dir / "identity.key"), log_level="critical",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        wait_for_server(server)
        ticket = context.pairing.create_ticket()
        device = DiscoveredDevice(
            id=context.identity.id, name="Receiver", host="127.0.0.1", port=port,
            fingerprint=context.identity.fingerprint, code_hash=hash_code(ticket.code),
            last_seen=time.time(),
        )
        sender_dir = tmp_path / "sender"
        manager = TransferManager(
            sender_dir, SettingsStore(sender_dir),
            DeviceIdentity(id="sender", name="Sender", fingerprint="A" * 64),
            cast(DiscoveryManager, FixedDiscovery(device)),
        )
        with pytest.raises(PeerConnectionError, match="Chứng chỉ HTTPS.*địa chỉ IP"):
            manager.pair(ticket.code)
        assert context.pairing.ticket == ticket

        with pytest.raises(PeerConnectionError, match="fingerprint did not match"):
            manager.pair(
                ticket.code, endpoint=f"https://127.0.0.1:{port}",
                fingerprint="00:" * 31 + "00",
            )
        assert context.pairing.ticket == ticket
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_same_code_pairs_and_transfers_from_multiple_peers(tmp_path: Path) -> None:
    port = open_port()
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
        ticket = receiver.pairing.create_ticket()
        device = DiscoveredDevice(
            id=receiver.identity.id, name=receiver.identity.name, host="127.0.0.1", port=port,
            fingerprint=receiver.identity.fingerprint, code_hash=ticket.code_hash,
            last_seen=time.time(),
        )
        managers = []
        for index in range(3):
            directory = tmp_path / f"sender-{index}"
            managers.append(TransferManager(
                directory, SettingsStore(directory),
                DeviceIdentity(
                    id=f"sender-{index}", name=f"Sender {index}", fingerprint=str(index) * 64,
                ),
                cast(DiscoveryManager, FixedDiscovery(device)),
            ))

        with ThreadPoolExecutor(max_workers=3) as pool:
            pairings = [pool.submit(manager.pair, ticket.code) for manager in managers]
            peers = [pairing.result(timeout=15) for pairing in pairings]
            assert len({peer.session_token for peer in peers}) == 3
            assert receiver.pairing.ticket == ticket

            transfers = []
            contents = []
            for index, (manager, peer) in enumerate(zip(managers, peers, strict=True)):
                content = f"content from sender {index}".encode() * 1000
                contents.append(content)
                staged = asyncio.run(manager.stage("message.txt", stream_bytes(content)))
                transfers.append(manager.start_outgoing(peer.id, "shared batch", [staged.id]))
            jobs = [
                pool.submit(manager.run_outgoing, transfer.id)
                for manager, transfer in zip(managers, transfers, strict=True)
            ]
            for job in jobs:
                job.result(timeout=20)

        destinations = []
        for index, (manager, transfer, content) in enumerate(
            zip(managers, transfers, contents, strict=True)
        ):
            assert manager.list_outgoing()[0].status == "complete"
            received = receiver.transfers.incoming_status(transfer.id)
            assert received.status == "complete"
            target = received.items[0].target
            destinations.append(target)
            assert target.read_bytes() == content
            authorized = receiver.sessions.verify(peers[index].session_token)
            assert authorized is not None and authorized.id == f"sender-{index}"
        assert len(set(destinations)) == 3

        assert managers[0].disconnect_peer(peers[0].id)
        assert receiver.sessions.verify(peers[0].session_token) is None
        for peer in peers[1:]:
            assert receiver.sessions.verify(peer.session_token) is not None

        # Rotating the shared code closes admission through the old code, while
        # the remaining connected peers can still send with their own tokens.
        receiver.pairing.create_ticket()
        with pytest.raises(PeerConnectionError, match="not correct"):
            managers[0].pair(ticket.code)
        remaining = managers[1]
        staged = asyncio.run(remaining.stage("after-disconnect.txt", stream_bytes(b"still paired")))
        transfer = remaining.start_outgoing(peers[1].id, "another batch", [staged.id])
        remaining.run_outgoing(transfer.id)
        received = receiver.transfers.incoming_status(transfer.id)
        assert received.status == "complete"
        assert (Path(received.destination) / "after-disconnect.txt").read_bytes() == b"still paired"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
