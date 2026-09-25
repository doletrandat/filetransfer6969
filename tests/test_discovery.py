from __future__ import annotations

import time
from typing import cast

from zeroconf import Zeroconf

from relay.discovery import DiscoveredDevice, DiscoveryManager


def test_devices_remain_visible_through_short_discovery_gaps() -> None:
    manager = DiscoveryManager("local", "Local", "192.168.1.10", 8765, "A" * 64)
    manager._devices["remote"] = DiscoveredDevice(
        id="remote",
        name="Remote",
        host="192.168.1.11",
        port=8765,
        fingerprint="B" * 64,
        code_hash="",
        last_seen=time.time() - 30,
    )

    assert [device.id for device in manager.list_devices()] == ["remote"]


def test_discovery_refresh_survives_a_reset_from_mdns() -> None:
    class ResettingZeroconf:
        def get_service_info(self, service_type: str, name: str, timeout: int) -> None:
            raise ConnectionResetError("peer reset")

    manager = DiscoveryManager("local", "Local", "192.168.1.10", 8765, "A" * 64)
    manager._zeroconf = cast(Zeroconf, ResettingZeroconf())

    manager.refresh_service("Remote._relay._tcp.local.")

    assert manager.list_devices() == []
