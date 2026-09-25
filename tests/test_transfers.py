from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, cast

from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveredDevice, DiscoveryManager
from relay.models import DeviceMessage, IncomingManifestRequest, TransferManifestItem
from relay.transfers import TransferManager


class EmptyDiscovery:
    def list_devices(self) -> list[DiscoveredDevice]:
        return []

    def find_by_code(self, code_hash: str) -> list[DiscoveredDevice]:
        return []


async def stream_bytes(value: bytes) -> Any:
    for offset in range(0, len(value), 4):
        yield value[offset : offset + 4]


def test_staging_and_incoming_file_are_verified(tmp_path: Path) -> None:
    manager = TransferManager(
        tmp_path / "data",
        SettingsStore(tmp_path / "data"),
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    content = b"relay file content"
    staged = asyncio.run(manager.stage("folder/file.bin", stream_bytes(content)))
    manifest = IncomingManifestRequest(
        batch_name="folder",
        source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
        items=[
            TransferManifestItem(
                id=staged.id,
                relative_path=staged.relative_path,
                size=staged.size,
                sha256=staged.sha256,
            )
        ],
    )

    incoming = manager.create_incoming(manifest)
    asyncio.run(manager.receive_file(incoming.transfer_id, staged.id, stream_bytes(content)))
    status = manager.incoming_status(incoming.transfer_id)
    received_path = Path(status.destination) / "folder" / "file.bin"

    assert received_path.read_bytes() == content
    assert status.status == "complete"
    assert status.received_bytes == len(content)
    assert hashlib.sha256(content).hexdigest() == staged.sha256


def test_chunked_upload_retries_a_completed_chunk(tmp_path: Path) -> None:
    manager = TransferManager(
        tmp_path / "data",
        SettingsStore(tmp_path / "data"),
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    content = b"0123456789abcdef"
    upload_id = "upload-1234567890"

    first = asyncio.run(
        manager.stage_chunk(
            upload_id, "large.bin", len(content), 0, False, stream_bytes(content[:8])
        )
    )
    duplicate = asyncio.run(
        manager.stage_chunk(
            upload_id, "large.bin", len(content), 0, False, stream_bytes(content[:8])
        )
    )
    final = asyncio.run(
        manager.stage_chunk(
            upload_id, "large.bin", len(content), 8, True, stream_bytes(content[8:])
        )
    )

    assert first["received"] == 8
    assert duplicate["received"] == 8
    assert final["complete"] is True
    assert final["item"]["size"] == len(content)
    assert manager.list_staged()[0].public_dict() == final["item"]
