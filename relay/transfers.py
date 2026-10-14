"""Điều phối truyền tệp và thực hiện luồng gửi sang máy tính khác."""
from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

import httpx

from relay.config import DeviceIdentity, SettingsStore
from relay.discovery import DiscoveryManager
from relay.models import (
    DeviceMessage,
    IncomingManifestRequest,
    IncomingManifestResponse,
    TransferManifestItem,
)
from relay.network import PeerConnectionError, open_peer_client
from relay.receiving import ReceivingService
from relay.staging import StagingService
from relay.transfer_paths import device_folder_name as device_folder_name
from relay.transfer_paths import safe_join as safe_join
from relay.transfer_paths import sanitize_relative_path as sanitize_relative_path
from relay.transfer_store import TransferStore
from relay.transfer_types import CHUNK_SIZE as CHUNK_SIZE
from relay.transfer_types import MAX_FILE_SIZE as MAX_FILE_SIZE
from relay.transfer_types import (
    ChunkedUpload,
    IncomingTransfer,
    OutgoingTransfer,
    PeerSession,
    ReceiveOffer,
    StagedItem,
)
from relay.transfer_types import TransferCancelled as TransferCancelled
from relay.transfer_types import TransferError as TransferError
from relay.transfer_types import UnsafePathError as UnsafePathError


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
        self._store = TransferStore(
            self._state_path, self._staging_dir, self._staged, self._outgoing, self._incoming,
        )
        self.staging = StagingService(
            self._staging_dir, self._staged, self._uploads, self._lock, self._save_state,
        )
        self.receiving = ReceivingService(
            self._settings, self._incoming, self._offers, self._lock, self._save_state,
        )
        self._load_state()
        for orphan in self._staging_dir.glob(".*.part"):
            orphan.unlink(missing_ok=True)

    async def stage(self, relative_path: str, stream: AsyncIterable[bytes]) -> StagedItem:
        return await self.staging.stage(relative_path, stream)

    async def stage_chunk(
        self,
        upload_id: str,
        relative_path: str,
        total_size: int,
        offset: int,
        final: bool,
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        return await self.staging.stage_chunk(
            upload_id, relative_path, total_size, offset, final, stream,
        )

    def list_staged(self) -> list[StagedItem]:
        return self.staging.list_staged()

    def remove_staged(self, item_id: str) -> bool:
        return self.staging.remove_staged(item_id)

    def clear_staged(self) -> int:
        return self.staging.clear_staged()

    def request_incoming(self, request: IncomingManifestRequest) -> ReceiveOffer:
        return self.receiving.request_incoming(request)

    def list_offers(self) -> list[ReceiveOffer]:
        return self.receiving.list_offers()

    def verify_offer(self, transfer_id: str, token: str) -> ReceiveOffer:
        return self.receiving.verify_offer(transfer_id, token)

    def decide_offer(self, transfer_id: str, accept: bool) -> ReceiveOffer:
        return self.receiving.decide_offer(transfer_id, accept)

    def cancel_offer(self, transfer_id: str) -> dict[str, Any]:
        return self.receiving.cancel_offer(transfer_id)

    def create_incoming(
        self, request: IncomingManifestRequest, *, transfer_id: str | None = None,
    ) -> IncomingManifestResponse:
        return self.receiving.create_incoming(request, transfer_id=transfer_id)

    async def receive_file(
        self,
        transfer_id: str,
        item_id: str,
        stream: AsyncIterable[bytes],
    ) -> None:
        return await self.receiving.receive_file(transfer_id, item_id, stream)

    async def receive_chunk(
        self,
        transfer_id: str,
        item_id: str,
        offset: int,
        total_size: int,
        final: bool,
        stream: AsyncIterable[bytes],
    ) -> dict[str, Any]:
        return await self.receiving.receive_chunk(
            transfer_id, item_id, offset, total_size, final, stream,
        )

    def incoming_status(self, transfer_id: str) -> IncomingTransfer:
        return self.receiving.incoming_status(transfer_id)

    def require_incoming_peer(self, transfer_id: str, peer_id: str, fingerprint: str) -> None:
        return self.receiving.require_incoming_peer(transfer_id, peer_id, fingerprint)

    def list_incoming(self) -> list[IncomingTransfer]:
        return self.receiving.list_incoming()

    def cancel_incoming(self, transfer_id: str) -> IncomingTransfer:
        return self.receiving.cancel_incoming(transfer_id)

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
        self._store.save()

    def _load_state(self) -> None:
        self._store.load()


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
