from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveredDevice, DiscoveryManager
from relay.models import (
    DeviceMessage,
    IncomingManifestRequest,
    IncomingManifestResponse,
    StagedItemMessage,
    TransferManifestItem,
)
from relay.network import PeerConnectionError, open_peer_client
from relay.security import SESSION_TTL_SECONDS, hash_code

CHUNK_SIZE = 1024 * 1024
MAX_FILE_SIZE = 1024 * 1024 * 1024 * 1024
INVALID_WINDOWS_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


class TransferError(RuntimeError):
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


def allocate_batch_destination(root: Path, batch_name: str) -> Path:
    safe_name = sanitize_relative_path(batch_name).name
    candidate = root / safe_name
    suffix = 2
    while candidate.exists():
        candidate = root / f"{safe_name} ({suffix})"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def safe_join(root: Path, relative_path: str) -> Path:
    relative = sanitize_relative_path(relative_path)
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise UnsafePathError("The selected path is not safe to transfer.")
    return candidate


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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
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
        self._peers: dict[str, PeerSession] = {}
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

    def pair(
        self,
        code: str,
        endpoint: str | None = None,
        fingerprint: str | None = None,
    ) -> PeerSession:
        code_hash = hash_code(code)
        if endpoint and fingerprint:
            parsed = urlparse(endpoint)
            if not parsed.hostname or not parsed.port:
                raise PeerConnectionError("That QR code has an invalid device address.")
            candidates = [
                DiscoveredDevice(
                    id="qr-peer",
                    name="Nearby device",
                    host=parsed.hostname,
                    port=parsed.port,
                    fingerprint=fingerprint,
                    code_hash=code_hash,
                    last_seen=time.time(),
                )
            ]
        else:
            candidates = self._discovery.find_by_code(code_hash)
        if not candidates:
            raise PeerConnectionError(
                "No nearby device is broadcasting that code. Check the code and try again."
            )
        last_error: Exception | None = None
        for device in candidates:
            endpoint = f"https://{device.host}:{device.port}"
            try:
                identity, client = open_peer_client(endpoint, device.fingerprint)
                with client:
                    response = client.post(
                        f"{endpoint}/api/v1/remote/pair",
                        json={
                            "code": code,
                            "device": self._identity_message().model_dump(),
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                remote = DeviceMessage.model_validate(payload["device"])
                if device.id != "qr-peer" and (
                    remote.id != device.id
                    or remote.fingerprint.upper() != device.fingerprint.upper()
                ):
                    raise PeerConnectionError(
                        "The nearby device identity did not match its announcement."
                    )
                session = PeerSession(
                    id=remote.id,
                    name=remote.name,
                    endpoint=endpoint,
                    fingerprint=remote.fingerprint,
                    session_token=payload["session_token"],
                    expires_at=time.time() + SESSION_TTL_SECONDS,
                )
                with self._lock:
                    self._peers[session.id] = session
                return session
            except httpx.HTTPStatusError as error:
                raise PeerConnectionError(
                    _response_error(error, "The pairing code was not accepted.")
                ) from error
            except (httpx.HTTPError, ValueError, KeyError) as error:
                last_error = error
        if isinstance(last_error, PeerConnectionError):
            raise last_error
        raise PeerConnectionError(f"The pairing code was not accepted. {last_error or ''}".strip())

    def list_peers(self) -> list[PeerSession]:
        now = time.time()
        with self._lock:
            expired = [peer_id for peer_id, peer in self._peers.items() if peer.expires_at <= now]
            for peer_id in expired:
                del self._peers[peer_id]
            return sorted(self._peers.values(), key=lambda peer: peer.name.lower())

    def start_outgoing(
        self,
        peer_id: str,
        batch_name: str,
        item_ids: list[str],
    ) -> OutgoingTransfer:
        with self._lock:
            peer = self._peers.get(peer_id)
            items = [self._staged[item_id] for item_id in item_ids if item_id in self._staged]
        if peer is None or peer.expires_at <= time.time():
            raise TransferError("Pair with that device again before sending.")
        if len(items) != len(set(item_ids)):
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
            raise PeerConnectionError("The device identity changed. Pair again.")
        try:
            response = client.post(
                f"{peer.endpoint}/api/v1/remote/transfers",
                headers={"Authorization": f"Bearer {peer.session_token}"},
                json={
                    "batch_name": batch_name,
                    "source": self._identity_message().model_dump(),
                    "items": manifest,
                },
            )
            response.raise_for_status()
            remote = IncomingManifestResponse.model_validate(response.json())
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
            completed_item_ids=remote.completed_item_ids,
            sent_bytes=sum(item.size for item in items if item.id in remote.completed_item_ids),
            sample_bytes=sum(item.size for item in items if item.id in remote.completed_item_ids),
            destination=remote.destination,
            status="sending",
        )
        with self._lock:
            self._outgoing[transfer.id] = transfer
            self._save_state()
        return transfer

    def run_outgoing(self, transfer_id: str) -> None:
        with self._lock:
            transfer = self._outgoing.get(transfer_id)
            peer = self._peers.get(transfer.peer_id) if transfer else None
            items = [self._staged.get(item_id) for item_id in transfer.item_ids] if transfer else []
        if transfer is None:
            return
        if peer is None or any(item is None for item in items):
            transfer.status = "failed"
            transfer.error = (
                "Pair with the receiver again and make sure the selected files are available."
            )
            with self._lock:
                self._save_state()
            return
        available_items = [item for item in items if item is not None]
        transfer.status = "sending"
        transfer.error = ""
        transfer.updated_at = time.time()
        try:
            identity, client = open_peer_client(peer.endpoint, peer.fingerprint)
            if DeviceMessage.model_validate(identity).id != peer.id:
                raise PeerConnectionError("The device identity changed. Pair again.")
            with client:
                status_response = client.get(
                    f"{peer.endpoint}/api/v1/remote/transfers/{transfer.id}",
                    headers={"Authorization": f"Bearer {peer.session_token}"},
                )
                status_response.raise_for_status()
                remote_status = status_response.json()
                transfer.completed_item_ids = list(remote_status.get("completed_item_ids", []))
                transfer.sent_bytes = sum(
                    item.size for item in available_items if item.id in transfer.completed_item_ids
                )
                transfer.sample_bytes = transfer.sent_bytes
                transfer.sample_at = time.monotonic()
                for item in available_items:
                    if item.id in transfer.completed_item_ids:
                        continue
                    transfer.current_item_id = item.id
                    transfer.updated_at = time.time()
                    item_offset = 0
                    with item.path.open("rb") as content:
                        while item_offset < item.size or (item.size == 0 and item_offset == 0):
                            content.seek(item_offset)
                            chunk = content.read(CHUNK_SIZE)
                            is_final = item_offset + len(chunk) >= item.size
                            last_error: Exception | None = None
                            response = None
                            for attempt in range(3):
                                try:
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
                                    last_error = None
                                    break
                                except httpx.HTTPError as error:
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
                transfer.completed_item_ids = list(final_status.get("completed_item_ids", []))
                transfer.sent_bytes = max(
                    transfer.sent_bytes,
                    sum(
                        item.size
                        for item in available_items
                        if item.id in transfer.completed_item_ids
                    ),
                )
            transfer.current_item_id = ""
            transfer.status = "complete"
            self._clear_completed_staging(transfer.item_ids, transfer.completed_item_ids)
        except (httpx.HTTPError, PeerConnectionError, TransferError, ValueError) as error:
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
            if transfer.status not in {"failed", "cancelled"}:
                raise TransferError("Only failed transfers can be retried.")
            if transfer.peer_id not in self._peers:
                raise TransferError("Pair with the receiver again before retrying.")
        self.run_outgoing(transfer_id)
        with self._lock:
            return self._outgoing[transfer_id]

    def list_outgoing(self) -> list[OutgoingTransfer]:
        with self._lock:
            return sorted(self._outgoing.values(), key=lambda item: item.created_at, reverse=True)

    def create_incoming(self, request: IncomingManifestRequest) -> IncomingManifestResponse:
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item.id) for item in request.items):
            raise TransferError("The transfer manifest contains an invalid file ID.")
        identifiers = [item.id for item in request.items]
        paths = [str(sanitize_relative_path(item.relative_path)) for item in request.items]
        if len(set(identifiers)) != len(identifiers) or len(set(paths)) != len(paths):
            raise TransferError("The transfer manifest contains duplicate files.")
        destination = allocate_batch_destination(
            self._settings.load().destination, request.batch_name
        )
        items: list[IncomingItem] = []
        for item in request.items:
            target = safe_join(destination, item.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            completed = (
                target.exists()
                and target.stat().st_size == item.size
                and _sha256(target) == item.sha256.lower()
            )
            items.append(
                IncomingItem(
                    id=item.id,
                    relative_path=item.relative_path,
                    size=item.size,
                    sha256=item.sha256.lower(),
                    target=target,
                    completed=completed,
                    received_bytes=item.size if completed else 0,
                )
            )
        transfer = IncomingTransfer(
            id=uuid.uuid4().hex,
            batch_name=request.batch_name,
            source=request.source,
            destination=destination,
            items=items,
            status="complete" if all(item.completed for item in items) else "receiving",
        )
        with self._lock:
            self._incoming[transfer.id] = transfer
            self._save_state()
        return IncomingManifestResponse(
            transfer_id=transfer.id,
            completed_item_ids=transfer.completed_item_ids,
            destination=str(destination),
        )

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
                with part_path.open("r+b") as output:
                    output.truncate(offset)
                item.received_bytes = offset
                item.digest = previous_digest
                raise
            new_size = item.received_bytes
        if not (final or new_size == item.size):
            transfer.updated_at = time.time()
            with self._lock:
                self._save_state()
            return {"received_bytes": new_size, "complete": False}
        if new_size != item.size or item.digest.hexdigest() != item.sha256:
            raise TransferError(f"{item.relative_path} failed its integrity check.")
        os.replace(part_path, item.target)
        item.completed = True
        if all(candidate.completed for candidate in transfer.items):
            transfer.status = "complete"
        transfer.updated_at = time.time()
        with self._lock:
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
                    sent_bytes=int(record["sent_bytes"]),
                    completed_item_ids=list(record["completed_item_ids"]),
                    destination=str(record["destination"]),
                    status="complete" if record["status"] == "complete" else "failed",
                    error=(
                        "" if record["status"] == "complete"
                        else "Relay restarted. Pair with the receiver again, then retry."
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
                    status="complete" if all(item.completed for item in items) else "waiting",
                    created_at=float(record["created_at"]),
                )
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
