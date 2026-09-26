from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveredDevice, DiscoveryManager
from relay.models import DeviceMessage, IncomingManifestRequest, TransferManifestItem
from relay.transfers import TransferError, TransferManager


class EmptyDiscovery:
    def list_devices(self) -> list[DiscoveredDevice]:
        return []

    def find_by_code(self, code_hash: str) -> list[DiscoveredDevice]:
        return []


async def stream_bytes(value: bytes) -> Any:
    for offset in range(0, len(value), 4):
        yield value[offset : offset + 4]


async def interrupted_stream(value: bytes) -> Any:
    yield value
    raise OSError("Connection interrupted")


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


def test_incoming_uses_receive_root_without_creating_batch_folders(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "data")
    root = settings.load().destination
    (root / "report.txt").write_bytes(b"existing")
    manager = TransferManager(
        tmp_path / "data", settings,
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )

    def manifest(item_id: str, relative_path: str, content: bytes) -> IncomingManifestRequest:
        return IncomingManifestRequest(
            batch_name="batch",
            source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
            items=[TransferManifestItem(
                id=item_id, relative_path=relative_path, size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )],
        )

    first = manager.create_incoming(manifest("file-1", "report.txt", b"new"))
    asyncio.run(manager.receive_file(first.transfer_id, "file-1", stream_bytes(b"new")))
    assert Path(first.destination) == root
    assert (root / "report.txt").read_bytes() == b"existing"
    assert (root / "report (2).txt").read_bytes() == b"new"

    duplicate = manager.create_incoming(manifest("file-2", "report.txt", b"new"))
    assert duplicate.completed_item_ids == ["file-2"]
    assert not (root / "report (3).txt").exists()

    nested = manager.create_incoming(manifest("file-3", "project/notes.txt", b"notes"))
    asyncio.run(manager.receive_file(nested.transfer_id, "file-3", stream_bytes(b"notes")))
    assert (root / "project" / "notes.txt").read_bytes() == b"notes"
    assert not (root / "batch").exists()
    assert not (root / "project (2)").exists()


def test_concurrent_incoming_transfers_reserve_distinct_files(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "data")
    manager = TransferManager(
        tmp_path / "data", settings,
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    source = DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64)
    transfers = []
    for item_id, content in (("file-1", b"first"), ("file-2", b"second")):
        request = IncomingManifestRequest(
            batch_name="batch", source=source,
            items=[TransferManifestItem(
                id=item_id, relative_path="note.txt", size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )],
        )
        transfers.append((manager.create_incoming(request), item_id, content))
    for transfer, item_id, content in transfers:
        asyncio.run(manager.receive_file(transfer.transfer_id, item_id, stream_bytes(content)))
    root = settings.load().destination
    assert (root / "note.txt").read_bytes() == b"first"
    assert (root / "note (2).txt").read_bytes() == b"second"


def test_incoming_does_not_replace_a_file_created_during_transfer(tmp_path: Path) -> None:
    settings = SettingsStore(tmp_path / "data")
    manager = TransferManager(
        tmp_path / "data", settings,
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    content = b"remote content"
    manifest = IncomingManifestRequest(
        batch_name="batch",
        source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
        items=[TransferManifestItem(
            id="file-1", relative_path="note.txt", size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )],
    )
    transfer = manager.create_incoming(manifest)
    root = settings.load().destination
    (root / "note.txt").write_bytes(b"local content")
    asyncio.run(manager.receive_file(transfer.transfer_id, "file-1", stream_bytes(content)))
    assert (root / "note.txt").read_bytes() == b"local content"
    assert (root / "note (2).txt").read_bytes() == content


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


def test_chunked_upload_recovers_after_interrupted_stream(tmp_path: Path) -> None:
    manager = TransferManager(
        tmp_path / "data",
        SettingsStore(tmp_path / "data"),
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    content = b"first chunk then second chunk"
    upload_id = "upload-interrupted"

    with pytest.raises(OSError, match="Connection interrupted"):
        asyncio.run(
            manager.stage_chunk(
                upload_id, "file.bin", len(content), 0, False, interrupted_stream(content[:11])
            )
        )
    first = asyncio.run(
        manager.stage_chunk(
            upload_id, "file.bin", len(content), 0, False, stream_bytes(content[:11])
        )
    )
    final = asyncio.run(
        manager.stage_chunk(
            upload_id, "file.bin", len(content), 11, True, stream_bytes(content[11:])
        )
    )

    assert first["received"] == 11
    assert final["item"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_incoming_chunk_retry_and_interruption_keep_integrity(tmp_path: Path) -> None:
    manager = TransferManager(
        tmp_path / "data",
        SettingsStore(tmp_path / "data"),
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    content = b"verified incoming file"
    item_id = "file-1"
    manifest = IncomingManifestRequest(
        batch_name="batch",
        source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
        items=[
            TransferManifestItem(
                id=item_id,
                relative_path="file.bin",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    transfer = manager.create_incoming(manifest)

    first = asyncio.run(
        manager.receive_chunk(
            transfer.transfer_id, item_id, 0, len(content), False, stream_bytes(content[:8])
        )
    )
    duplicate = asyncio.run(
        manager.receive_chunk(
            transfer.transfer_id, item_id, 0, len(content), False, stream_bytes(content[:8])
        )
    )
    with pytest.raises(OSError, match="Connection interrupted"):
        asyncio.run(
            manager.receive_chunk(
                transfer.transfer_id,
                item_id,
                8,
                len(content),
                False,
                interrupted_stream(content[8:13]),
            )
        )
    final = asyncio.run(
        manager.receive_chunk(
            transfer.transfer_id, item_id, 8, len(content), True, stream_bytes(content[8:])
        )
    )

    assert first["received_bytes"] == duplicate["received_bytes"] == 8
    assert final["complete"] is True
    status = manager.incoming_status(transfer.transfer_id)
    assert (status.destination / "file.bin").read_bytes() == content


def test_incoming_chunk_rejects_invalid_digest(tmp_path: Path) -> None:
    manager = TransferManager(
        tmp_path / "data",
        SettingsStore(tmp_path / "data"),
        DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
        cast(DiscoveryManager, EmptyDiscovery()),
    )
    manifest = IncomingManifestRequest(
        batch_name="batch",
        source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
        items=[
            TransferManifestItem(
                id="file-1",
                relative_path="file.bin",
                size=4,
                sha256=hashlib.sha256(b"good").hexdigest(),
            )
        ],
    )
    transfer = manager.create_incoming(manifest)

    with pytest.raises(TransferError, match="failed its integrity check"):
        asyncio.run(
            manager.receive_chunk(transfer.transfer_id, "file-1", 0, 4, True, stream_bytes(b"evil"))
        )

    assert not (Path(transfer.destination) / "file.bin").exists()


def test_staged_and_partial_incoming_survive_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    def manager() -> TransferManager:
        return TransferManager(
            data_dir, SettingsStore(data_dir),
            DeviceIdentity(id="local", name="Local PC", fingerprint="A" * 64),
            cast(DiscoveryManager, EmptyDiscovery()),
        )

    first = manager()
    content = b"content that spans two chunks"
    staged = asyncio.run(first.stage("folder/file.bin", stream_bytes(content)))
    manifest = IncomingManifestRequest(
        batch_name="batch",
        source=DeviceMessage(id="remote", name="Remote PC", fingerprint="B" * 64),
        items=[TransferManifestItem(
            id=staged.id, relative_path=staged.relative_path,
            size=staged.size, sha256=staged.sha256,
        )],
    )
    transfer = first.create_incoming(manifest)
    asyncio.run(first.receive_chunk(
        transfer.transfer_id, staged.id, 0, len(content), False, stream_bytes(content[:8])
    ))
    target = first.incoming_status(transfer.transfer_id).items[0].target
    part_path = target.with_name(f".{target.name}.{staged.id}.part")
    with part_path.open("ab") as output:
        output.write(b"uncommitted bytes")

    restored = manager()
    assert restored.list_staged()[0].id == staged.id
    assert restored.incoming_status(transfer.transfer_id).status == "waiting"
    assert restored.incoming_status(transfer.transfer_id).received_bytes == 8
    assert part_path.stat().st_size == 8
    duplicate = asyncio.run(restored.receive_chunk(
        transfer.transfer_id, staged.id, 0, len(content), False, stream_bytes(content[:8])
    ))
    assert duplicate["received_bytes"] == 8
    asyncio.run(restored.receive_chunk(
        transfer.transfer_id, staged.id, 8, len(content), True, stream_bytes(content[8:])
    ))
    assert (Path(transfer.destination) / "folder" / "file.bin").read_bytes() == content
