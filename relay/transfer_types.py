"""Cấu trúc dữ liệu, trạng thái và lỗi dùng chung khi truyền tệp."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from relay.models import DeviceMessage, IncomingManifestRequest, StagedItemMessage

CHUNK_SIZE = 1024 * 1024
MAX_FILE_SIZE = 1024**4


class TransferError(RuntimeError):
    pass

class TransferCancelled(TransferError):
    pass

class UnsafePathError(TransferError):
    pass

class Digest(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...

    def copy(self) -> Digest: ...

@dataclass(frozen=True, slots=True)
class StagedItem:
    id: str
    relative_path: str
    size: int
    sha256: str
    staged_at: float
    path: Path

    def public_dict(self) -> dict[str, Any]:
        return StagedItemMessage(
            id=self.id,
            relative_path=self.relative_path,
            size=self.size,
            sha256=self.sha256,
            staged_at=self.staged_at,
        ).model_dump()

@dataclass(slots=True)
class ChunkedUpload:
    upload_id: str
    relative_path: str
    total_size: int
    size: int
    path: Path
    created_at: float
    item: StagedItem | None = None
    digest: Digest = field(default_factory=hashlib.sha256, repr=False)

@dataclass(frozen=True, slots=True)
class PeerSession:
    id: str
    name: str
    endpoint: str
    fingerprint: str
    session_token: str
    expires_at: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at,
        }

@dataclass(slots=True)
class OutgoingTransfer:
    id: str
    batch_name: str
    peer_id: str
    peer_name: str
    item_ids: list[str]
    total_bytes: int
    retain_staged: bool = False
    sent_bytes: int = 0
    speed_bps: float = 0
    completed_item_ids: list[str] = field(default_factory=list)
    current_item_id: str = ""
    status: str = "preparing"
    error: str = ""
    destination: str = ""
    sample_at: float = field(default_factory=time.monotonic, repr=False)
    sample_bytes: int = field(default=0, repr=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch_name": self.batch_name,
            "peer_id": self.peer_id,
            "peer_name": self.peer_name,
            "item_ids": self.item_ids,
            "total_bytes": self.total_bytes,
            "retain_staged": self.retain_staged,
            "sent_bytes": self.sent_bytes,
            "speed_bps": self.speed_bps,
            "completed_item_ids": self.completed_item_ids,
            "current_item_id": self.current_item_id,
            "status": self.status,
            "error": self.error,
            "destination": self.destination,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

@dataclass(slots=True)
class IncomingItem:
    id: str
    relative_path: str
    size: int
    sha256: str
    target: Path
    completed: bool = False
    received_bytes: int = 0
    digest: Digest = field(default_factory=hashlib.sha256, repr=False)

@dataclass(slots=True)
class IncomingTransfer:
    id: str
    batch_name: str
    source: DeviceMessage
    destination: Path
    items: list[IncomingItem]
    status: str = "receiving"
    error: str = ""
    speed_bps: float = 0
    sample_at: float = field(default_factory=time.monotonic, repr=False)
    sample_bytes: int = field(default=0, repr=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.items)

    @property
    def received_bytes(self) -> int:
        return sum(item.received_bytes for item in self.items)

    @property
    def completed_item_ids(self) -> list[str]:
        return [item.id for item in self.items if item.completed]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch_name": self.batch_name,
            "source": self.source.model_dump(),
            "destination": str(self.destination),
            "status": self.status,
            "error": self.error,
            "total_bytes": self.total_bytes,
            "received_bytes": self.received_bytes,
            "speed_bps": self.speed_bps,
            "completed_item_ids": self.completed_item_ids,
            "items": [
                {
                    "id": item.id,
                    "name": item.target.name,
                    "relative_path": item.relative_path,
                    "size": item.size,
                    "completed": item.completed,
                    "available": (
                        item.completed and item.target.is_file() and not item.target.is_symlink()
                    ),
                }
                for item in self.items
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

@dataclass(slots=True)
class ReceiveOffer:
    id: str
    manifest: IncomingManifestRequest
    token: str
    expires_at: float
    status: str = "waiting"
    created_at: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.manifest.source.model_dump(),
            "batch_name": self.manifest.batch_name, "status": self.status,
            "file_count": len(self.manifest.items),
            "total_bytes": sum(item.size for item in self.manifest.items),
            "items": [item.model_dump() for item in self.manifest.items],
            "expires_at": self.expires_at, "created_at": self.created_at,
        }
