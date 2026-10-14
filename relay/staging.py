"""Chuẩn bị tệp từ trình duyệt trước khi gửi tới thiết bị khác."""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from _thread import RLock
from collections.abc import AsyncIterable, Callable
from pathlib import Path
from typing import Any

from relay.transfer_paths import sanitize_relative_path
from relay.transfer_types import MAX_FILE_SIZE, ChunkedUpload, StagedItem, TransferError


class StagingService:
    def __init__(
        self,
        staging_dir: Path,
        staged: dict[str, StagedItem],
        uploads: dict[str, ChunkedUpload],
        lock: RLock,
        save_state: Callable[[], None],
    ) -> None:
        self._staging_dir = staging_dir
        self._staged = staged
        self._uploads = uploads
        self._lock = lock
        self._save_state = save_state

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
