from __future__ import annotations

import hashlib
import hmac
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
from pathlib import Path
from typing import Any

from relay.transfers import (
    MAX_FILE_SIZE,
    TransferCancelled,
    TransferError,
    safe_join,
    sanitize_relative_path,
)

INVITE_TTL_SECONDS = 600
PHONE_SESSION_TTL_SECONDS = 3600
PHONE_COOKIE_NAME = "relay_phone_session"
PHONE_ACTIVE_SECONDS = 15


@dataclass(slots=True)
class PhoneSession:
    csrf_token: str
    expires_at: float
    connected_at: float = 0.0
    last_seen_at: float = 0.0
    uploaded: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PhoneUpload:
    id: str
    name: str
    total_size: int
    session: PhoneSession = field(repr=False)
    received_bytes: int = 0
    status: str = "receiving"
    error: str = ""
    part_path: Path | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.total_size,
            "received_bytes": self.received_bytes,
            "status": self.status,
            "error": self.error,
            "received_at": self.created_at,
            "source": "phone",
        }


class PhoneAccess:
    def __init__(self, data_dir: Path) -> None:
        self._lock = threading.RLock()
        self._history_path = data_dir / "phone-uploads.json"
        try:
            loaded = json.loads(self._history_path.read_text(encoding="utf-8"))
            self._history: list[dict[str, Any]] = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self._history = []
        self._invite_digest: str | None = None
        self._invite_expires_at = 0.0
        self._sessions: dict[str, PhoneSession] = {}
        self._reserved_targets: set[str] = set()
        self._transfers: dict[str, PhoneUpload] = {}

    def connection_state(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            active = [
                session for session in self._sessions.values()
                if session.expires_at > now and now - session.last_seen_at <= PHONE_ACTIVE_SECONDS
            ]
            if active:
                return {
                    "status": "connected",
                    "connected_at": max(session.connected_at for session in active),
                }
            if self._invite_digest is not None and self._invite_expires_at > now:
                return {"status": "waiting", "connected_at": None}
            return {"status": "disconnected", "connected_at": None}

    def create_invitation(self) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        expiry = time.time() + INVITE_TTL_SECONDS
        with self._lock:
            self._invite_digest = self._digest(token)
            self._invite_expires_at = expiry
            self._sessions.clear()
        return token, expiry

    def redeem(self, token: str) -> tuple[str, PhoneSession] | None:
        with self._lock:
            digest = self._invite_digest
            if (
                digest is None
                or self._invite_expires_at <= time.time()
                or not hmac.compare_digest(digest, self._digest(token))
            ):
                return None
            self._invite_digest = None
            self._invite_expires_at = 0.0
            cookie = secrets.token_urlsafe(32)
            now = time.time()
            session = PhoneSession(
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + PHONE_SESSION_TTL_SECONDS,
                connected_at=now,
                last_seen_at=now,
            )
            self._sessions[self._digest(cookie)] = session
            return cookie, session

    def valid_invitation(self, token: str) -> bool:
        with self._lock:
            return (
                self._invite_digest is not None
                and self._invite_expires_at > time.time()
                and hmac.compare_digest(self._invite_digest, self._digest(token))
            )

    def get_session(self, cookie: str | None) -> PhoneSession | None:
        if not cookie:
            return None
        digest = self._digest(cookie)
        with self._lock:
            session = self._sessions.get(digest)
            if session is None:
                return None
            if session.expires_at <= time.time():
                del self._sessions[digest]
                return None
            session.last_seen_at = time.time()
            return session

    def revoke(self) -> None:
        with self._lock:
            self._invite_digest = None
            self._invite_expires_at = 0.0
            self._sessions.clear()
            active = [
                upload.id for upload in self._transfers.values()
                if upload.status == "receiving"
            ]
        for upload_id in active:
            self.cancel_upload(upload_id)

    def uploaded_files(self, session: PhoneSession) -> list[dict[str, Any]]:
        with self._lock:
            return [item.copy() for item in session.uploaded]

    def recent_uploads(self) -> list[dict[str, Any]]:
        with self._lock:
            uploads = [item.copy() for item in self._history[:50]]
        for item in uploads:
            path = Path(str(item.get("path", "")))
            try:
                item["available"] = path.is_file() and not path.is_symlink()
            except OSError:
                item["available"] = False
        return uploads

    def transfer_activity(self) -> list[dict[str, Any]]:
        with self._lock:
            transfers = [
                upload.public_dict()
                for upload in self._transfers.values()
                if upload.status != "complete"
            ]
        return sorted(transfers, key=lambda item: item["received_at"], reverse=True)

    def cancel_upload(
        self, upload_id: str, session: PhoneSession | None = None
    ) -> PhoneUpload:
        with self._lock:
            upload = self._transfers.get(upload_id)
            if upload is None or (session is not None and upload.session is not session):
                raise TransferError("That phone transfer is no longer available.")
            if upload.status != "receiving":
                raise TransferError("Only an active phone transfer can be cancelled.")
            upload.status = "cancelled"
            upload.error = "This phone transfer was cancelled."
            upload.updated_at = time.time()
            part_path = upload.part_path
        if part_path is not None:
            with suppress(PermissionError):
                part_path.unlink(missing_ok=True)
        return upload

    def import_existing(self, destination_root: Path) -> None:
        """Make files received by older Relay versions visible in the desktop inbox."""
        with self._lock:
            if self._history:
                return
            found: list[dict[str, Any]] = []
            try:
                folders = destination_root.glob("From phone*")
                for folder in folders:
                    if (
                        not re.fullmatch(r"From phone(?: \(\d+\))?", folder.name)
                        or not folder.is_dir()
                    ):
                        continue
                    for path in folder.iterdir():
                        if not path.is_file() or path.name.endswith(".part"):
                            continue
                        info = path.stat()
                        found.append({
                            "id": uuid.uuid5(uuid.NAMESPACE_URL, str(path)).hex,
                            "name": path.name,
                            "size": info.st_size,
                            "folder": folder.name,
                            "path": str(path),
                            "received_at": info.st_mtime,
                        })
            except OSError as error:
                print(f"Could not inspect earlier phone uploads: {error}")
                return
            self._history = sorted(found, key=lambda item: item["received_at"], reverse=True)[:50]
            if self._history:
                self._save_history()

    async def receive_file(
        self,
        session: PhoneSession,
        destination_root: Path,
        filename: str,
        stream: AsyncIterable[bytes],
        *,
        upload_id: str | None = None,
        total_size: int | None = None,
    ) -> dict[str, Any]:
        upload_id = upload_id or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", upload_id):
            raise TransferError("The phone transfer ID is invalid.")
        if total_size is not None and (total_size < 0 or total_size > MAX_FILE_SIZE):
            raise TransferError("The selected file is too large.")
        with self._lock:
            if upload_id in self._transfers:
                raise TransferError("That phone transfer is already in progress.")
            target, part_path = self._reserve_target(destination_root, filename)
            upload = PhoneUpload(
                id=upload_id,
                name=target.name,
                total_size=total_size if total_size is not None else 0,
                session=session,
                part_path=part_path,
            )
            terminal = sorted(
                (
                    candidate for candidate in self._transfers.values()
                    if candidate.status != "receiving"
                ),
                key=lambda candidate: candidate.updated_at,
                reverse=True,
            )
            for expired in terminal[50:]:
                self._transfers.pop(expired.id, None)
            self._transfers[upload_id] = upload
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("xb") as output:
                async for chunk in stream:
                    if upload.status == "cancelled":
                        raise TransferCancelled(upload.error)
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise TransferError("The selected file is too large.")
                    if total_size is not None and size > total_size:
                        raise TransferError("The upload is larger than the selected file.")
                    output.write(chunk)
                    digest.update(chunk)
                    upload.received_bytes = size
                    upload.updated_at = time.time()
            if upload.status == "cancelled":
                raise TransferCancelled(upload.error)
            if total_size is not None and size != total_size:
                raise TransferError("The phone upload ended before the file was complete.")
            with self._lock:
                if target.exists():
                    self._reserved_targets.discard(str(target).casefold())
                    target = self._available_target(target.parent, sanitize_relative_path(filename))
                    self._reserved_targets.add(str(target).casefold())
                os.replace(part_path, target)
                result: dict[str, Any] = {
                    "id": uuid.uuid4().hex,
                    "name": target.name,
                    "size": size,
                    "sha256": digest.hexdigest(),
                    "folder": target.parent.name,
                    "path": str(target),
                    "received_at": time.time(),
                }
                session.uploaded.append(result)
                self._history.insert(0, result)
                self._history = self._history[:50]
                self._save_history()
                upload.status = "complete"
                upload.updated_at = time.time()
            return result
        except TransferCancelled:
            upload.status = "cancelled"
            upload.error = upload.error or "This phone transfer was cancelled."
            upload.updated_at = time.time()
            raise
        except Exception:
            if upload.status == "receiving":
                upload.status = "failed"
                upload.error = "The phone transfer was interrupted."
                upload.updated_at = time.time()
            raise
        finally:
            part_path.unlink(missing_ok=True)
            upload.part_path = None
            with self._lock:
                self._reserved_targets.discard(str(target).casefold())

    def _save_history(self) -> None:
        try:
            temporary = self._history_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._history), encoding="utf-8")
            os.replace(temporary, self._history_path)
        except OSError as error:
            print(f"Could not save phone upload history: {error}")

    def _reserve_target(
        self, destination_root: Path, filename: str
    ) -> tuple[Path, Path]:
        safe_name = sanitize_relative_path(filename)
        if len(safe_name.parts) != 1:
            raise TransferError("Choose a file, not a path.")
        with self._lock:
            folder = safe_join(destination_root, "From phone")
            folder.mkdir(parents=True, exist_ok=True)
            target = self._available_target(folder, safe_name)
            self._reserved_targets.add(str(target).casefold())
            part_path = folder / f".{uuid.uuid4().hex}.part"
            return target, part_path

    def _available_target(self, folder: Path, safe_name: Path) -> Path:
        candidate = safe_name.name
        suffix = 2
        while (
            str(folder / candidate).casefold() in self._reserved_targets
            or (folder / candidate).exists()
        ):
            candidate = f"{safe_name.stem[:200]} ({suffix}){safe_name.suffix}"
            suffix += 1
        return safe_join(folder, candidate)

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
