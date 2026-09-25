from __future__ import annotations

import pytest

from relay.security import AuthorizedPeer, PairingError, PairingRegistry


def test_pairing_code_can_only_be_redeemed_once() -> None:
    registry = PairingRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="A" * 64)

    token = registry.redeem(ticket.code, peer)

    assert token
    with pytest.raises(PairingError, match="expired"):
        registry.redeem(ticket.code, peer)


def test_pairing_code_rejects_wrong_attempts() -> None:
    registry = PairingRegistry()
    ticket = registry.create_ticket()
    peer = AuthorizedPeer(id="device-1", name="Desk PC", fingerprint="B" * 64)

    with pytest.raises(PairingError, match="not correct"):
        registry.redeem("WRONG123", peer)

    assert registry.redeem(ticket.code, peer)
