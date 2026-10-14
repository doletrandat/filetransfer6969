"""Chấp nhận lượt gửi, nhận từng khối và kiểm tra tệp hoàn tất."""
from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from _thread import RLock
from collections.abc import AsyncIterable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from relay.config import SettingsStore
from relay.models import IncomingManifestRequest, IncomingManifestResponse
from relay.transfer_paths import _sha256, device_folder_name, safe_join, sanitize_relative_path
from relay.transfer_types import (
    MAX_FILE_SIZE,
    IncomingItem,
    IncomingTransfer,
    ReceiveOffer,
    TransferCancelled,
    TransferError,
)


class ReceivingService:
    def __init__(
        self,
        settings: SettingsStore,
        incoming: dict[str, IncomingTransfer],
        offers: dict[str, ReceiveOffer],
        lock: RLock,
        save_state: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._incoming = incoming
        self._offers = offers
        self._lock = lock
        self._save_state = save_state

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
