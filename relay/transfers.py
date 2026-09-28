from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import httpx

from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveryManager
from relay.models import (
    DeviceMessage,
    IncomingManifestRequest,
    IncomingManifestResponse,
    StagedItemMessage,
    TransferManifestItem,
)
from relay.network import PeerConnectionError, open_peer_client

CHUNK_SIZE = 1024 * 1024
MAX_FILE_SIZE = 1024 * 1024 * 1024 * 1024
INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


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


def sanitize_relative_path(value: str) -> Path:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise UnsafePathError("The selected path is not safe to transfer.")
    safe_parts: list[str] = []
    for part in path.parts:
        if part in {"", ".", ".."}:
            raise UnsafePathError("The selected path is not safe to transfer.")
        cleaned = INVALID_WINDOWS_CHARACTERS.sub("_", part).rstrip(" .")
        if not cleaned:
            cleaned = "_"
        if cleaned.upper() in RESERVED_WINDOWS_NAMES:
            cleaned = f"_{cleaned}"
        safe_parts.append(cleaned[:240])
    if not safe_parts:
        raise UnsafePathError("The selected path is not safe to transfer.")
    return Path(*safe_parts)


def safe_join(root: Path, relative_path: str) -> Path:
    relative = sanitize_relative_path(relative_path)
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise UnsafePathError("The selected path is not safe to transfer.")
    return candidate


def device_folder_name(name: str) -> str:
    cleaned = INVALID_WINDOWS_CHARACTERS.sub("_", name).strip(" .")[:80].rstrip(" .")
    if not cleaned:
        cleaned = "Relay device"
    if cleaned.split(".")[0].upper() in RESERVED_WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


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


class TransferManager:
    def __init__(
        self,
        data_dir: Path,
        settings: SettingsStore,
        identity: DeviceIdentity,
        discovery: DiscoveryManager,
    ) -> None:
        self._data_dir = data_dir
        self._settings = settings
        self._identity = identity
        self._discovery = discovery
        self._staging_dir = data_dir / "staging"
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "transfer-state.json"
        self._staged: dict[str, StagedItem] = {}
        self._uploads: dict[str, ChunkedUpload] = {}
        self._outgoing: dict[str, OutgoingTransfer] = {}
        self._incoming: dict[str, IncomingTransfer] = {}
        self._offers: dict[str, ReceiveOffer] = {}
        self._credentials: dict[str, PeerSession] = {}
        self._lock = threading.RLock()
        self._load_state()
        for orphan in self._staging_dir.glob(".*.part"):
            orphan.unlink(missing_ok=True)

    async def stage(self, relative_path: str, stream: AsyncIterable[bytes]) -> StagedItem:
        safe_path = sanitize_relative_path(relative_path)
        item_id = uuid.uuid4().hex
        stored_path = self._staging_dir / item_id
        digest = hashlib.sha256()
        size = 0
        try:
            with stored_path.open("wb") as output:
                async for chunk in stream:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise TransferError("This file is larger than Relay supports.")
                    digest.update(chunk)
                    output.write(chunk)
            item = StagedItem(
                id=item_id,
                relative_path=str(safe_path),
                size=size,
                sha256=digest.hexdigest(),
                staged_at=time.time(),
                path=stored_path,
            )
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise
        with self._lock:
            self._staged[item_id] = item
            self._save_state()
        return item

    async def stage_chunk(
        self,
        upload_id: str,
        relative_path: str,
        total_size: int,
        offset: int,
        final: bool,
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        safe_path = sanitize_relative_path(relative_path)
        if total_size < 0 or total_size > MAX_FILE_SIZE:
            raise TransferError("This file is larger than Relay supports.")
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is None:
                if offset != 0:
                    raise TransferError(
                        "This upload expired before it finished. Choose the file again."
                    )
                upload = ChunkedUpload(
                    upload_id=upload_id,
                    relative_path=str(safe_path),
                    total_size=total_size,
                    size=0,
                    path=self._staging_dir / f".{upload_id}.part",
                    created_at=time.time(),
                )
                self._uploads[upload_id] = upload
            elif upload.relative_path != str(safe_path) or upload.total_size != total_size:
                raise TransferError("The upload metadata does not match the current file.")
        if upload.item is not None:
            return {
                "received": upload.item.size,
                "complete": True,
                "item": upload.item.public_dict(),
            }
        current_size = upload.size
        if offset > current_size:
            raise TransferError("The upload offset is ahead of Relay. Retry the current chunk.")
        if offset < current_size:
            data = bytearray()
            async for chunk in stream:
                data.extend(chunk)
                if len(data) > 64 * 1024 * 1024:
                    raise TransferError("This upload chunk is too large.")
            with upload.path.open("rb") as existing:
                existing.seek(offset)
                stored = existing.read(len(data))
            if stored != bytes(data):
                raise TransferError("The retried upload chunk did not match the original data.")
            new_size = current_size
        else:
            previous_digest = upload.digest.copy()
            try:
                with upload.path.open("ab") as output:
                    async for chunk in stream:
                        if not chunk:
                            continue
                        new_size = upload.size + len(chunk)
                        if new_size > total_size:
                            raise TransferError(
                                "The upload contains more data than the selected file."
                            )
                        output.write(chunk)
                        upload.digest.update(chunk)
                        upload.size = new_size
            except Exception:
                with upload.path.open("r+b") as output:
                    output.truncate(offset)
                upload.size = offset
                upload.digest = previous_digest
                raise
            new_size = upload.size
        if new_size > total_size:
            raise TransferError("The upload contains more data than the selected file.")
        if not (final or new_size == total_size):
            return {"received": new_size, "complete": False, "item": None}
        if new_size != total_size:
            raise TransferError("The upload ended before the selected file was complete.")
        item_id = uuid.uuid4().hex
        stored_path = self._staging_dir / item_id
        os.replace(upload.path, stored_path)
        item = StagedItem(
            id=item_id,
            relative_path=upload.relative_path,
            size=upload.size,
            sha256=upload.digest.hexdigest(),
            staged_at=time.time(),
            path=stored_path,
        )
        with self._lock:
            upload.item = item
            self._staged[item_id] = item
            self._save_state()
        return {"received": item.size, "complete": True, "item": item.public_dict()}

    def list_staged(self) -> list[StagedItem]:
        with self._lock:
            return sorted(self._staged.values(), key=lambda item: item.staged_at)

    def remove_staged(self, item_id: str) -> bool:
        with self._lock:
            item = self._staged.pop(item_id, None)
            if item is not None:
                self._save_state()
        if item is None:
            return False
        item.path.unlink(missing_ok=True)
        return True

    def clear_staged(self) -> int:
        with self._lock:
            items = list(self._staged.values())
            self._staged.clear()
            self._save_state()
        for item in items:
            item.path.unlink(missing_ok=True)
        return len(items)

    def start_outgoing(
        self,
        peer_id: str,
        batch_name: str,
        item_ids: list[str],
        *,
        retain_staged: bool = False,
    ) -> OutgoingTransfer:
        with self._lock:
            device = next((d for d in self._discovery.list_devices() if d.id == peer_id), None)
            items = [self._staged[item_id] for item_id in item_ids if item_id in self._staged]
        if device is None:
            raise TransferError("Không tìm thấy máy nhận. Hãy mở Relay trên máy đó.")
        peer = PeerSession(device.id, device.name, f"https://{device.host}:{device.port}",
                           device.fingerprint, "", time.time() + 86400)
        if not items or len(items) != len(item_ids) or len(set(item_ids)) != len(item_ids):
            raise TransferError("One or more selected files are no longer staged.")
        if len({item.relative_path for item in items}) != len(items):
            raise TransferError("The transfer contains the same path more than once.")
        manifest = [
            TransferManifestItem(
                id=item.id,
                relative_path=item.relative_path,
                size=item.size,
                sha256=item.sha256,
            ).model_dump()
            for item in items
        ]
        identity, client = open_peer_client(peer.endpoint, peer.fingerprint)
        remote_identity = DeviceMessage.model_validate(identity)
        if remote_identity.id != peer.id:
            client.close()
            raise PeerConnectionError("Định danh máy nhận đã thay đổi. Hãy tìm lại thiết bị.")
        try:
            response = client.post(
                f"{peer.endpoint}/api/v1/remote/transfers",
                json={
                    "batch_name": batch_name,
                    "source": self._identity_message().model_dump(),
                    "items": manifest,
                },
            )
            response.raise_for_status()
            offer = response.json()
            if not isinstance(offer, dict) or not offer.get("id") or not offer.get("token"):
                raise TransferError("Hãy cập nhật Relay trên cả hai máy để dùng xác nhận nhận tệp.")
            remote = IncomingManifestResponse(
                transfer_id=offer["id"], completed_item_ids=[], destination="",
            )
            peer = PeerSession(peer.id, peer.name, peer.endpoint, peer.fingerprint,
                               offer["token"], peer.expires_at)
        except (httpx.HTTPError, ValueError) as error:
            raise TransferError(
                _response_error(error, "The receiver could not prepare the transfer.")
            ) from error
        finally:
            client.close()
        transfer = OutgoingTransfer(
            id=remote.transfer_id,
            batch_name=batch_name,
            peer_id=peer.id,
            peer_name=peer.name,
            item_ids=[item.id for item in items],
            total_bytes=sum(item.size for item in items),
            retain_staged=retain_staged,
            completed_item_ids=remote.completed_item_ids,
            sent_bytes=sum(item.size for item in items if item.id in remote.completed_item_ids),
            sample_bytes=sum(item.size for item in items if item.id in remote.completed_item_ids),
            destination=remote.destination,
            status="waiting",
        )
        with self._lock:
            self._outgoing[transfer.id] = transfer
            self._credentials[transfer.id] = peer
            self._save_state()
        return transfer

    def run_outgoing(self, transfer_id: str) -> None:
        with self._lock:
            transfer = self._outgoing.get(transfer_id)
            peer = self._credentials.get(transfer_id)
            items = [self._staged.get(item_id) for item_id in transfer.item_ids] if transfer else []
        if transfer is None:
            return
        if transfer.status == "cancelled":
            return
        if peer is None or any(item is None for item in items):
            if transfer.status == "cancelled":
                return
            transfer.status = "failed"
            transfer.error = (
                "Hãy chọn lại máy nhận và kiểm tra các tệp đã chuẩn bị."
            )
            with self._lock:
                self._save_state()
            return
        available_items = [item for item in items if item is not None]
        with self._lock:
            if transfer.status == "cancelled":
                return
            transfer.status = "waiting"
            transfer.error = ""
            transfer.updated_at = time.time()
        try:
            self._require_outgoing_active(transfer)
            identity, client = open_peer_client(peer.endpoint, peer.fingerprint)
            if DeviceMessage.model_validate(identity).id != peer.id:
                raise PeerConnectionError("Định danh máy nhận đã thay đổi. Hãy tìm lại thiết bị.")
            with client:
                while True:
                    self._require_outgoing_active(transfer)
                    status_response = client.get(
                        f"{peer.endpoint}/api/v1/remote/transfers/{transfer.id}",
                        headers={"Authorization": f"Bearer {peer.session_token}"},
                    )
                    status_response.raise_for_status()
                    remote_status = status_response.json()
                    if remote_status.get("status") != "waiting":
                        break
                    time.sleep(0.3)
                self._require_outgoing_active(transfer)
                if remote_status.get("status") in {"rejected", "expired"}:
                    transfer.status = remote_status["status"]
                    transfer.error = (
                        "Máy nhận đã từ chối lượt gửi." if transfer.status == "rejected"
                        else "Yêu cầu đã hết hạn. Hãy gửi lại."
                    )
                    return
                if remote_status.get("status") == "cancelled":
                    raise TransferCancelled("The receiver cancelled this transfer.")
                transfer.status = "sending"
                transfer.destination = remote_status.get("destination", "")
                transfer.completed_item_ids = list(remote_status.get("completed_item_ids", []))
                transfer.sent_bytes = sum(
                    item.size for item in available_items if item.id in transfer.completed_item_ids
                )
                transfer.sample_bytes = transfer.sent_bytes
                transfer.sample_at = time.monotonic()
                for item in available_items:
                    self._require_outgoing_active(transfer)
                    if item.id in transfer.completed_item_ids:
                        continue
                    transfer.current_item_id = item.id
                    transfer.updated_at = time.time()
                    item_offset = 0
                    with item.path.open("rb") as content:
                        while item_offset < item.size or (item.size == 0 and item_offset == 0):
                            self._require_outgoing_active(transfer)
                            content.seek(item_offset)
                            chunk = content.read(CHUNK_SIZE)
                            is_final = item_offset + len(chunk) >= item.size
                            last_error: Exception | None = None
                            response = None
                            for attempt in range(3):
                                try:
                                    self._require_outgoing_active(transfer)
                                    response = client.post(
                                        f"{peer.endpoint}/api/v1/remote/transfers/{transfer.id}/files/{item.id}/chunks",
                                        headers={
                                            "Authorization": f"Bearer {peer.session_token}",
                                            "Content-Type": "application/octet-stream",
                                        },
                                        params={
                                            "offset": item_offset,
                                            "total_size": item.size,
                                            "final": is_final,
                                        },
                                        content=chunk,
                                    )
                                    response.raise_for_status()
                                    self._require_outgoing_active(transfer)
                                    last_error = None
                                    break
                                except TransferCancelled:
                                    raise
                                except httpx.HTTPError as error:
                                    if (
                                        isinstance(error, httpx.HTTPStatusError)
                                        and error.response.status_code == 409
                                    ):
                                        raise TransferCancelled(
                                            _response_error(
                                                error, "The receiver cancelled this transfer."
                                            )
                                        ) from error
                                    last_error = error
                                    if attempt < 2:
                                        time.sleep(2**attempt)
                            if last_error is not None:
                                raise TransferError(
                                    _response_error(
                                        last_error,
                                        f"Could not send {item.relative_path}.",
                                    )
                                ) from last_error
                            if response is None:
                                raise TransferError(f"Could not send {item.relative_path}.")
                            received = int(response.json().get("received_bytes", item_offset))
                            if received > item_offset:
                                self._record_outgoing_progress(transfer, received - item_offset)
                            item_offset = received
                            if response.json().get("complete"):
                                if item.id not in transfer.completed_item_ids:
                                    transfer.completed_item_ids.append(item.id)
                                break
                final_response = client.get(
                    f"{peer.endpoint}/api/v1/remote/transfers/{transfer.id}",
                    headers={"Authorization": f"Bearer {peer.session_token}"},
                )
                final_response.raise_for_status()
                final_status = final_response.json()
                if final_status.get("status") == "cancelled":
                    raise TransferCancelled("The receiver cancelled this transfer.")
                transfer.completed_item_ids = list(final_status.get("completed_item_ids", []))
                transfer.sent_bytes = max(
                    transfer.sent_bytes,
                    sum(
                        item.size
                        for item in available_items
                        if item.id in transfer.completed_item_ids
                    ),
                )
            self._require_outgoing_active(transfer)
            transfer.current_item_id = ""
            transfer.status = "complete"
            if not transfer.retain_staged:
                self._clear_completed_staging(transfer.item_ids, transfer.completed_item_ids)
        except TransferCancelled as error:
            transfer.status = "cancelled"
            transfer.error = str(error) or "This transfer was cancelled."
        except (httpx.HTTPError, PeerConnectionError, TransferError, ValueError, OSError) as error:
            if transfer.status != "cancelled":
                transfer.status = "failed"
                transfer.error = str(error)
        finally:
            transfer.updated_at = time.time()
            with self._lock:
                self._save_state()

    def retry_outgoing(self, transfer_id: str) -> OutgoingTransfer:
        with self._lock:
            transfer = self._outgoing.get(transfer_id)
            if transfer is None:
                raise TransferError("That transfer is no longer available.")
            if transfer.status != "failed":
                raise TransferError("Only failed transfers can be retried.")
        self._cancel_remote(transfer)
        return self.start_outgoing(transfer.peer_id, transfer.batch_name, transfer.item_ids,
                                   retain_staged=transfer.retain_staged)

    def list_outgoing(self) -> list[OutgoingTransfer]:
        with self._lock:
            return sorted(self._outgoing.values(), key=lambda item: item.created_at, reverse=True)

    def cancel_outgoing(self, transfer_id: str) -> OutgoingTransfer:
        with self._lock:
            transfer = self._outgoing.get(transfer_id)
            if transfer is None:
                raise TransferError("That transfer is no longer available.")
            if transfer.status not in {"preparing", "sending", "waiting"}:
                raise TransferError("Only an active transfer can be cancelled.")
            transfer.status = "cancelled"
            transfer.error = "You cancelled this transfer."
            transfer.speed_bps = 0
            transfer.updated_at = time.time()
            self._save_state()
        self._cancel_remote(transfer)
        return transfer

    def _cancel_remote(self, transfer: OutgoingTransfer) -> None:
        peer = self._credentials.get(transfer.id)
        if peer is not None:
            try:
                identity, client = open_peer_client(peer.endpoint, peer.fingerprint)
                with client:
                    if DeviceMessage.model_validate(identity).id == peer.id:
                        response = client.delete(
                            f"{peer.endpoint}/api/v1/remote/transfers/{transfer.id}",
                            headers={"Authorization": f"Bearer {peer.session_token}"},
                        )
                        if response.status_code not in {404, 409}:
                            response.raise_for_status()
            except (httpx.HTTPError, PeerConnectionError, ValueError):
                # The local cancellation remains authoritative. If the peer is still
                # receiving, the sender loop stops before another chunk is sent.
                pass

    @staticmethod
    def _require_outgoing_active(transfer: OutgoingTransfer) -> None:
        if transfer.status == "cancelled":
            raise TransferCancelled(transfer.error or "This transfer was cancelled.")

    def request_incoming(self, request: IncomingManifestRequest) -> ReceiveOffer:
        self._validate_manifest(request)
        with self._lock:
            self.list_offers()
            if sum(offer.status == "waiting" for offer in self._offers.values()) >= 32:
                raise TransferError("Máy nhận đang có quá nhiều yêu cầu. Hãy thử lại sau.")
            if len(self._offers) >= 256:
                removable = next((key for key, offer in self._offers.items()
                                  if offer.status in {"rejected", "expired", "cancelled"}
                                  or (key in self._incoming
                                      and self._incoming[key].status in {"complete", "cancelled"})),
                                 None)
                if removable is None:
                    raise TransferError("Máy nhận đang bận. Hãy thử lại sau.")
                del self._offers[removable]
            offer = ReceiveOffer(uuid.uuid4().hex, request.model_copy(deep=True),
                                 secrets.token_urlsafe(32), time.time() + 120)
            self._offers[offer.id] = offer
            return offer

    def list_offers(self) -> list[ReceiveOffer]:
        with self._lock:
            for offer in self._offers.values():
                if offer.status == "waiting" and offer.expires_at <= time.time():
                    offer.status = "expired"
            return list(self._offers.values())

    def verify_offer(self, transfer_id: str, token: str) -> ReceiveOffer:
        with self._lock:
            self.list_offers()
            offer = self._offers.get(transfer_id)
            if offer is None or not secrets.compare_digest(offer.token, token):
                raise TransferError("Quyền truy cập lượt gửi không hợp lệ.")
            return offer

    def decide_offer(self, transfer_id: str, accept: bool) -> ReceiveOffer:
        with self._lock:
            self.list_offers()
            offer = self._offers.get(transfer_id)
            if offer is None or offer.status != "waiting":
                raise TransferError("Yêu cầu đã được xử lý hoặc hết hạn.")
            if accept:
                self.create_incoming(offer.manifest, transfer_id=offer.id)
                offer.status = "accepted"
            else:
                offer.status = "rejected"
            return offer

    def cancel_offer(self, transfer_id: str) -> dict[str, Any]:
        with self._lock:
            offer = self._offers[transfer_id]
            if offer.status == "accepted":
                return self.cancel_incoming(transfer_id).public_dict()
            if offer.status == "waiting":
                offer.status = "cancelled"
            return offer.public_dict()

    @staticmethod
    def _validate_manifest(request: IncomingManifestRequest) -> None:
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item.id) for item in request.items):
            raise TransferError("The transfer manifest contains an invalid file ID.")
        identifiers = [item.id for item in request.items]
        paths = [
            str(sanitize_relative_path(item.relative_path)).casefold()
            for item in request.items
        ]
        if len(set(identifiers)) != len(identifiers) or len(set(paths)) != len(paths):
            raise TransferError("The transfer manifest contains duplicate files.")

    def create_incoming(
        self, request: IncomingManifestRequest, *, transfer_id: str | None = None,
    ) -> IncomingManifestResponse:
        self._validate_manifest(request)
        with self._lock:
            destination = safe_join(
                self._settings.load().destination, device_folder_name(request.source.name)
            )
            reserved = {
                str(item.target).casefold()
                for transfer in self._incoming.values()
                if transfer.status != "cancelled"
                for item in transfer.items
                if not item.completed
            }
            items: list[IncomingItem] = []
            for item in request.items:
                digest = item.sha256.lower()
                target, completed = self._received_target(
                    destination, item.relative_path, item.size, digest, reserved
                )
                if not completed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    reserved.add(str(target).casefold())
                items.append(IncomingItem(
                    id=item.id,
                    relative_path=str(target.relative_to(destination)),
                    size=item.size,
                    sha256=digest,
                    target=target,
                    completed=completed,
                    received_bytes=item.size if completed else 0,
                ))
            transfer = IncomingTransfer(
                id=transfer_id or uuid.uuid4().hex,
                batch_name=request.batch_name,
                source=request.source,
                destination=destination,
                items=items,
                status="complete" if all(item.completed for item in items) else "receiving",
            )
            self._incoming[transfer.id] = transfer
            self._save_state()
        return IncomingManifestResponse(
            transfer_id=transfer.id,
            completed_item_ids=transfer.completed_item_ids,
            destination=str(destination),
        )

    @staticmethod
    def _received_target(
        root: Path, relative_path: str, size: int, digest: str, reserved: set[str]
    ) -> tuple[Path, bool]:
        original = sanitize_relative_path(relative_path)
        candidate_path = original
        suffix = 2
        while True:
            candidate = safe_join(root, str(candidate_path))
            if str(candidate).casefold() not in reserved:
                if candidate.is_file():
                    if candidate.stat().st_size == size and _sha256(candidate) == digest:
                        return candidate, True
                elif not candidate.exists():
                    return candidate, False
            candidate_path = original.with_name(
                f"{original.stem[:200]} ({suffix}){original.suffix}"
            )
            suffix += 1

    async def receive_file(
        self,
        transfer_id: str,
        item_id: str,
        stream: AsyncIterable[bytes],
    ) -> None:
        item = self._incoming_item(transfer_id, item_id)
        await self.receive_chunk(
            transfer_id,
            item_id,
            offset=0,
            total_size=item.size,
            final=True,
            stream=stream,
        )

    async def receive_chunk(
        self,
        transfer_id: str,
        item_id: str,
        offset: int,
        total_size: int,
        final: bool,
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        with self._lock:
            transfer = self._incoming.get(transfer_id)
            item = (
                next((candidate for candidate in transfer.items if candidate.id == item_id), None)
                if transfer
                else None
            )
        if transfer is None or item is None:
            raise TransferError("That incoming file is no longer available.")
        if transfer.status == "cancelled":
            raise TransferCancelled(transfer.error or "This transfer was cancelled.")
        transfer.status = "receiving"
        if total_size != item.size:
            raise TransferError("The incoming file size does not match its manifest.")
        if item.completed:
            return {"received_bytes": item.size, "complete": True}
        if offset > item.received_bytes:
            raise TransferError("The incoming chunk offset is ahead of Relay.")
        part_path = item.target.with_name(f".{item.target.name}.{item.id}.part")
        if offset < item.received_bytes:
            data = bytearray()
            async for chunk in stream:
                data.extend(chunk)
                if len(data) > 64 * 1024 * 1024:
                    raise TransferError("This transfer chunk is too large.")
            with part_path.open("rb") as existing:
                existing.seek(offset)
                stored = existing.read(len(data))
            if stored != bytes(data):
                raise TransferError("The retried transfer chunk did not match the original data.")
            new_size = item.received_bytes
        else:
            previous_digest = item.digest.copy()
            try:
                with part_path.open("ab") as output:
                    async for chunk in stream:
                        if transfer.status == "cancelled":
                            raise TransferCancelled(
                                transfer.error or "This transfer was cancelled."
                            )
                        if not chunk:
                            continue
                        new_size = item.received_bytes + len(chunk)
                        if new_size > item.size or new_size > MAX_FILE_SIZE:
                            raise TransferError(f"{item.relative_path} is larger than announced.")
                        output.write(chunk)
                        item.digest.update(chunk)
                        item.received_bytes = new_size
                        self._record_incoming_progress(transfer, len(chunk))
            except Exception:
                if part_path.exists():
                    with part_path.open("r+b") as output:
                        output.truncate(offset)
                item.received_bytes = offset
                item.digest = previous_digest
                if transfer.status == "cancelled":
                    part_path.unlink(missing_ok=True)
                raise
            new_size = item.received_bytes
        if transfer.status == "cancelled":
            part_path.unlink(missing_ok=True)
            raise TransferCancelled(transfer.error or "This transfer was cancelled.")
        if not (final or new_size == item.size):
            transfer.updated_at = time.time()
            with self._lock:
                self._save_state()
            return {"received_bytes": new_size, "complete": False}
        if new_size != item.size or item.digest.hexdigest() != item.sha256:
            raise TransferError(f"{item.relative_path} failed its integrity check.")
        with self._lock:
            if item.target.exists():
                reserved = {
                    str(candidate.target).casefold()
                    for incoming in self._incoming.values()
                    if incoming.status != "cancelled"
                    for candidate in incoming.items
                    if candidate is not item and not candidate.completed
                }
                item.target, already_present = self._received_target(
                    transfer.destination, item.relative_path, item.size, item.sha256,
                    reserved,
                )
                item.relative_path = str(item.target.relative_to(transfer.destination))
                if already_present:
                    part_path.unlink()
                else:
                    os.replace(part_path, item.target)
            else:
                os.replace(part_path, item.target)
            item.completed = True
            if all(candidate.completed for candidate in transfer.items):
                transfer.status = "complete"
            transfer.updated_at = time.time()
            self._save_state()
        return {"received_bytes": item.size, "complete": True}

    def _incoming_item(self, transfer_id: str, item_id: str) -> IncomingItem:
        with self._lock:
            transfer = self._incoming.get(transfer_id)
            item = (
                next((candidate for candidate in transfer.items if candidate.id == item_id), None)
                if transfer
                else None
            )
        if transfer is None or item is None:
            raise TransferError("That incoming file is no longer available.")
        return item

    def incoming_status(self, transfer_id: str) -> IncomingTransfer:
        with self._lock:
            transfer = self._incoming.get(transfer_id)
        if transfer is None:
            raise TransferError("That incoming transfer is no longer available.")
        return transfer

    def require_incoming_peer(self, transfer_id: str, peer_id: str, fingerprint: str) -> None:
        transfer = self.incoming_status(transfer_id)
        if (
            transfer.source.id != peer_id
            or transfer.source.fingerprint.upper() != fingerprint.upper()
        ):
            raise TransferError("This transfer belongs to a different paired device.")

    def list_incoming(self) -> list[IncomingTransfer]:
        with self._lock:
            return sorted(self._incoming.values(), key=lambda item: item.created_at, reverse=True)

    def cancel_incoming(self, transfer_id: str) -> IncomingTransfer:
        with self._lock:
            transfer = self._incoming.get(transfer_id)
            if transfer is None:
                raise TransferError("That incoming transfer is no longer available.")
            if transfer.status not in {"receiving", "waiting"}:
                raise TransferError("Only an active transfer can be cancelled.")
            transfer.status = "cancelled"
            transfer.error = "The receiver cancelled this transfer."
            transfer.speed_bps = 0
            transfer.updated_at = time.time()
            partials = [
                item.target.with_name(f".{item.target.name}.{item.id}.part")
                for item in transfer.items
                if not item.completed
            ]
            self._save_state()
        for path in partials:
            # An in-flight request may still own the file on Windows. It observes
            # the cancelled status and removes the partial after closing it.
            with suppress(PermissionError):
                path.unlink(missing_ok=True)
        return transfer

    def _record_outgoing_progress(self, transfer: OutgoingTransfer, delta: int) -> None:
        now = time.monotonic()
        elapsed = max(now - transfer.sample_at, 0.05)
        transfer.sent_bytes += delta
        sample_delta = transfer.sent_bytes - transfer.sample_bytes
        instant_speed = sample_delta / elapsed
        transfer.speed_bps = (
            instant_speed
            if transfer.speed_bps <= 0
            else transfer.speed_bps * 0.65 + instant_speed * 0.35
        )
        transfer.sample_at = now
        transfer.sample_bytes = transfer.sent_bytes
        transfer.updated_at = time.time()

    def _record_incoming_progress(self, transfer: IncomingTransfer, delta: int) -> None:
        now = time.monotonic()
        elapsed = max(now - transfer.sample_at, 0.05)
        current = transfer.received_bytes
        transfer.speed_bps = (
            delta / elapsed
            if transfer.speed_bps <= 0
            else transfer.speed_bps * 0.65 + (delta / elapsed) * 0.35
        )
        transfer.sample_at = now
        transfer.sample_bytes = current
        transfer.updated_at = time.time()

    def _identity_message(self) -> DeviceMessage:
        return DeviceMessage(
            id=self._identity.id,
            name=self._identity.name,
            fingerprint=self._identity.fingerprint,
        )

    def _clear_completed_staging(self, item_ids: list[str], completed: list[str]) -> None:
        for item_id in set(item_ids) & set(completed):
            self.remove_staged(item_id)

    def _save_state(self) -> None:
        payload = {
            "version": 1,
            "staged": [item.public_dict() for item in self._staged.values()],
            "outgoing": [item.public_dict() for item in self._outgoing.values()],
            "incoming": [
                {
                    "id": transfer.id,
                    "batch_name": transfer.batch_name,
                    "source": transfer.source.model_dump(),
                    "destination": str(transfer.destination),
                    "created_at": transfer.created_at,
                    "status": transfer.status,
                    "error": transfer.error,
                    "items": [
                        {
                            "id": item.id,
                            "relative_path": item.relative_path,
                            "size": item.size,
                            "sha256": item.sha256,
                            "received_bytes": item.received_bytes,
                        }
                        for item in transfer.items
                    ],
                }
                for transfer in self._incoming.values()
            ],
        }
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self._state_path)

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("unsupported state version")
            for record in payload.get("staged", []):
                item_id = str(record["id"])
                if not re.fullmatch(r"[0-9a-f]{32}", item_id):
                    continue
                path = self._staging_dir / item_id
                size = int(record["size"])
                if not path.is_file() or path.stat().st_size != size:
                    continue
                self._staged[item_id] = StagedItem(
                    id=item_id,
                    relative_path=str(sanitize_relative_path(str(record["relative_path"]))),
                    size=size,
                    sha256=str(record["sha256"]),
                    staged_at=float(record["staged_at"]),
                    path=path,
                )
            for record in payload.get("outgoing", []):
                transfer = OutgoingTransfer(
                    id=str(record["id"]),
                    batch_name=str(record["batch_name"]),
                    peer_id=str(record["peer_id"]),
                    peer_name=str(record["peer_name"]),
                    item_ids=list(record["item_ids"]),
                    total_bytes=int(record["total_bytes"]),
                    retain_staged=bool(record.get("retain_staged", False)),
                    sent_bytes=int(record["sent_bytes"]),
                    completed_item_ids=list(record["completed_item_ids"]),
                    destination=str(record["destination"]),
                    status=(
                        record["status"] if record["status"] in {"complete", "rejected", "expired"}
                        else "cancelled" if record["status"] == "cancelled"
                        else "failed"
                    ),
                    error=(
                        str(record.get("error", ""))
                        if record["status"] in {"complete", "rejected", "expired"}
                        else "This transfer was cancelled."
                        if record["status"] == "cancelled"
                        else "Relay đã khởi động lại. Thử gửi lại để máy nhận đồng ý lần nữa."
                    ),
                    created_at=float(record["created_at"]),
                    updated_at=float(record["updated_at"]),
                )
                self._outgoing[transfer.id] = transfer
            for record in payload.get("incoming", []):
                destination = Path(str(record["destination"]))
                items = []
                for saved in record["items"]:
                    item_id = str(saved["id"])
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id):
                        raise ValueError("invalid saved file ID")
                    relative_path = str(sanitize_relative_path(str(saved["relative_path"])))
                    target = safe_join(destination, relative_path)
                    size = int(saved["size"])
                    sha256 = str(saved["sha256"])
                    completed = (
                        target.is_file() and target.stat().st_size == size
                        and _sha256(target) == sha256
                    )
                    part_path = target.with_name(f".{target.name}.{item_id}.part")
                    digest = hashlib.sha256()
                    received = 0
                    if not completed and part_path.is_file():
                        checkpoint = int(saved.get("received_bytes", 0))
                        received = min(part_path.stat().st_size, checkpoint)
                        if received < 0 or received > size:
                            raise ValueError("invalid saved partial-file checkpoint")
                        with part_path.open("r+b") as source:
                            source.truncate(received)
                        with part_path.open("rb") as source:
                            for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                                digest.update(chunk)
                    items.append(IncomingItem(
                        id=item_id, relative_path=relative_path, size=size,
                        sha256=sha256, target=target, completed=completed,
                        received_bytes=size if completed else received, digest=digest,
                    ))
                incoming_transfer = IncomingTransfer(
                    id=str(record["id"]), batch_name=str(record["batch_name"]),
                    source=DeviceMessage.model_validate(record["source"]),
                    destination=destination, items=items,
                    status=(
                        "complete" if all(item.completed for item in items)
                        else "cancelled" if record.get("status") == "cancelled"
                        else "waiting"
                    ),
                    error=str(record.get("error", "")),
                    created_at=float(record["created_at"]),
                )
                if incoming_transfer.status == "cancelled":
                    for item in items:
                        if not item.completed:
                            item.target.with_name(
                                f".{item.target.name}.{item.id}.part"
                            ).unlink(missing_ok=True)
                            item.received_bytes = 0
                self._incoming[incoming_transfer.id] = incoming_transfer
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"Relay could not restore transfer state: {error}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _response_error(error: Exception, fallback: str) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        try:
            payload = error.response.json()
            return str(payload.get("detail", fallback))
        except (ValueError, AttributeError):
            return fallback
    if isinstance(error, PeerConnectionError):
        return str(error)
    return fallback
