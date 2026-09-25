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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.transfers import (
    MAX_FILE_SIZE,
    TransferError,
    allocate_batch_destination,
    safe_join,
    sanitize_relative_path,
)

INVITE_TTL_SECONDS = 600
PHONE_SESSION_TTL_SECONDS = 3600
PHONE_COOKIE_NAME = "relay_phone_session"


@dataclass(slots=True)
class PhoneSession:
    csrf_token: str
    expires_at: float
    batch_dir: Path | None = None
    reserved_names: set[str] = field(default_factory=set)
    uploaded: list[dict[str, Any]] = field(default_factory=list)


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
            session = PhoneSession(
                csrf_token=secrets.token_urlsafe(32),
                expires_at=time.time() + PHONE_SESSION_TTL_SECONDS,
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
            return session

    def revoke(self) -> None:
        with self._lock:
            self._invite_digest = None
            self._invite_expires_at = 0.0
            self._sessions.clear()

    def uploaded_files(self, session: PhoneSession) -> list[dict[str, Any]]:
        with self._lock:
            return [item.copy() for item in session.uploaded]

    def recent_uploads(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.copy() for item in self._history[:50]]

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
    ) -> dict[str, Any]:
        target, part_path = self._reserve_target(session, destination_root, filename)
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("xb") as output:
                async for chunk in stream:
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise TransferError("The selected file is too large.")
                    output.write(chunk)
                    digest.update(chunk)
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
            with self._lock:
                session.uploaded.append(result)
                self._history.insert(0, result)
                self._history = self._history[:50]
                self._save_history()
            return result
        finally:
            part_path.unlink(missing_ok=True)
            with self._lock:
                session.reserved_names.discard(target.name.casefold())

    def _save_history(self) -> None:
        try:
            temporary = self._history_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._history), encoding="utf-8")
            os.replace(temporary, self._history_path)
        except OSError as error:
            print(f"Could not save phone upload history: {error}")

    def _reserve_target(
        self, session: PhoneSession, destination_root: Path, filename: str
    ) -> tuple[Path, Path]:
        safe_name = sanitize_relative_path(filename)
        if len(safe_name.parts) != 1:
            raise TransferError("Choose a file, not a path.")
        with self._lock:
            if session.batch_dir is None:
                session.batch_dir = allocate_batch_destination(destination_root, "From phone")
            folder = session.batch_dir
            candidate = safe_name.name
            suffix = 2
            while candidate.casefold() in session.reserved_names or (folder / candidate).exists():
                base = safe_name.stem[:200]
                candidate = f"{base} ({suffix}){safe_name.suffix}"
                suffix += 1
            target = safe_join(folder, candidate)
            session.reserved_names.add(target.name.casefold())
            part_path = folder / f".{uuid.uuid4().hex}.part"
            return target, part_path

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
