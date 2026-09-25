from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 3600
MAX_CODE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class PairingTicket:
    code: str
    code_hash: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class AuthorizedPeer:
    id: str
    name: str
    fingerprint: str


class PairingRegistry:
    def __init__(self) -> None:
        self._ticket: PairingTicket | None = None
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def create_ticket(self) -> PairingTicket:
        with self._lock:
            self._attempts.clear()
            code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
            ticket = PairingTicket(
                code=code,
                code_hash=hash_code(code),
                expires_at=time.time() + CODE_TTL_SECONDS,
            )
            self._ticket = ticket
            return ticket

    @property
    def ticket(self) -> PairingTicket | None:
        with self._lock:
            if self._ticket is None or self._ticket.expires_at <= time.time():
                return None
            return self._ticket

    def redeem(self, code: str, peer: AuthorizedPeer) -> str:
        now = time.time()
        with self._lock:
            ticket = self._ticket
            if ticket is None or ticket.expires_at <= now:
                raise PairingError("This pairing code has expired. Ask for a new code.")
            failures = self._attempts.setdefault(peer.fingerprint, [])
            failures[:] = [attempt for attempt in failures if now - attempt < 60]
            if len(failures) >= MAX_CODE_ATTEMPTS:
                raise PairingError("Too many attempts. Ask for a new pairing code.")
            if not hmac.compare_digest(hash_code(code), ticket.code_hash):
                failures.append(now)
                remaining = max(1, MAX_CODE_ATTEMPTS - len(failures))
                raise PairingError(f"That code is not correct. {remaining} attempts remain.")
            self._ticket = None
            return issue_session_token(peer)


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def issue_session_token(peer: AuthorizedPeer) -> str:
    return f"{peer.id}.{secrets.token_urlsafe(32)}"


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[AuthorizedPeer, float]] = {}
        self._lock = threading.Lock()

    def register(self, token: str, peer: AuthorizedPeer) -> None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._sessions[digest] = (peer, time.time() + SESSION_TTL_SECONDS)

    def verify(self, token: str) -> AuthorizedPeer | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            expired = [key for key, (_, expiry) in self._sessions.items() if expiry <= now]
            for key in expired:
                del self._sessions[key]
            entry = self._sessions.get(digest)
            if entry is None:
                return None
            peer, expiry = entry
            if expiry <= now:
                del self._sessions[digest]
                return None
            return peer


class PairingError(ValueError):
    pass
