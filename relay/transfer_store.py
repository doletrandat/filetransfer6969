"""Lưu và khôi phục hàng chờ cùng lịch sử truyền từ JSON."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from relay.models import DeviceMessage
from relay.transfer_paths import _sha256, safe_join, sanitize_relative_path
from relay.transfer_types import (
    CHUNK_SIZE,
    IncomingItem,
    IncomingTransfer,
    OutgoingTransfer,
    StagedItem,
)


class TransferStore:
    def __init__(
        self,
        state_path: Path,
        staging_dir: Path,
        staged: dict[str, StagedItem],
        outgoing: dict[str, OutgoingTransfer],
        incoming: dict[str, IncomingTransfer],
    ) -> None:
        self._state_path = state_path
        self._staging_dir = staging_dir
        self._staged = staged
        self._outgoing = outgoing
        self._incoming = incoming

    def save(self) -> None:
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

    def load(self) -> None:
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
