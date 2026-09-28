from __future__ import annotations

import pytest

from relay.security import (
    MAX_CODE_ATTEMPTS,
    AuthorizedPeer,
    PairingError,
    PairingRegistry,
    SessionRegistry,
)


def test_pairing_code_allows_multiple_independent_sessions() -> None:
    registry = PairingRegistry()
    sessions = SessionRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="A" * 64)
    other = AuthorizedPeer(id="device-2", name="Laptop", fingerprint="B" * 64)

    token = registry.redeem(ticket.code, peer)
    other_token = registry.redeem(ticket.code.lower(), other)
    sessions.register(token, peer)
    sessions.register(other_token, other)

    assert token != other_token
    assert registry.ticket == ticket
    assert sessions.verify(token) == peer
    assert sessions.verify(other_token) == other
    assert sessions.revoke_peer(peer) == 1
    assert sessions.verify(token) is None
    assert sessions.verify(other_token) == other


def test_code_rotation_preserves_existing_sessions() -> None:
    registry = PairingRegistry()
    sessions = SessionRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="A" * 64)
    token = registry.redeem(ticket.code, peer)
    sessions.register(token, peer)

    replacement = registry.create_ticket()
    with pytest.raises(PairingError, match="not correct"):
        registry.redeem(ticket.code, peer)
    assert registry.redeem(replacement.code, peer)
    assert sessions.verify(token) == peer


def test_reusing_code_does_not_extend_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = PairingRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="A" * 64)
    monkeypatch.setattr("relay.security.time.time", lambda: ticket.expires_at - 1)
    assert registry.redeem(ticket.code, peer)
    assert registry.ticket == ticket

    monkeypatch.setattr("relay.security.time.time", lambda: ticket.expires_at)
    with pytest.raises(PairingError, match="expired"):
        registry.redeem(ticket.code, peer)
    assert registry.ticket is None


def test_wrong_attempts_only_block_the_attempting_peer() -> None:
    registry = PairingRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="A" * 64)
    other = AuthorizedPeer(id="device-2", name="Laptop", fingerprint="B" * 64)
    for _ in range(MAX_CODE_ATTEMPTS):
        with pytest.raises(PairingError, match="not correct"):
            registry.redeem("WRONG123", peer)
    with pytest.raises(PairingError, match="Too many"):
        registry.redeem(ticket.code, peer)
    assert registry.redeem(ticket.code, other)


def test_pairing_code_rejects_wrong_attempts() -> None:
    registry = PairingRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="B" * 64)

    with pytest.raises(PairingError, match="not correct"):
        registry.redeem("WRONG123", peer)

    assert registry.redeem(ticket.code, peer)
