from __future__ import annotations

import socket
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

SERVICE_TYPE = "_relay._tcp.local."
STALE_AFTER_SECONDS = 90
REFRESH_INTERVAL_SECONDS = 15


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    id: str
    name: str
    host: str
    port: int
    fingerprint: str
    code_hash: str
    last_seen: float

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("code_hash", None)
        return payload


class DiscoveryListener(ServiceListener):
    def __init__(self, manager: DiscoveryManager) -> None:
        self._manager = manager

    def add_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self._manager.refresh_service(name)

    def update_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self._manager.refresh_service(name)

    def remove_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self._manager.remove_service(name)


class DiscoveryManager:
    def __init__(
        self,
        device_id: str,
        device_name: str,
        host: str,
        port: int,
        fingerprint: str,
        addresses: list[str] | None = None,
    ) -> None:
        self._device_id = device_id
        self._device_name = device_name
        self._host = host
        self._addresses = [
            address for address in (addresses or [host]) if not address.startswith("127.")
        ]
        self._port = port
        self._fingerprint = fingerprint
        self._code_hash = ""
        self._devices: dict[str, DiscoveredDevice] = {}
        self._known_services: set[str] = set()
        self._service_devices: dict[str, str] = {}
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._published_service: ServiceInfo | None = None
        self._registration_name = f"{self._safe_name(device_name)}-{device_id[:6]}.{SERVICE_TYPE}"

    @property
    def active(self) -> bool:
        with self._lifecycle_lock:
            return self._zeroconf is not None and not self._stop_event.is_set()

    def start(self, address: str) -> None:
        with self._lifecycle_lock:
            if self._zeroconf is not None:
                return
            self._stop_event.clear()
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            self._browser = ServiceBrowser(self._zeroconf, SERVICE_TYPE, DiscoveryListener(self))
            self._publish(address)
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name="relay-discovery-refresh",
                daemon=True,
            )
            self._refresh_thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            browser = self._browser
            zeroconf = self._zeroconf
            published = self._published_service
            self._browser = None
            self._zeroconf = None
            self._published_service = None
        if browser is not None:
            with suppress(Exception):
                browser.cancel()
        if zeroconf is not None and published is not None:
            with suppress(Exception):
                zeroconf.unregister_service(published)
        if zeroconf is not None:
            with suppress(Exception):
                zeroconf.close()
        thread = self._refresh_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._refresh_thread = None

    def update_code_hash(self, code_hash: str, address: str) -> None:
        with self._lock:
            self._code_hash = code_hash
        if self.active:
            try:
                self._publish(address)
            except Exception:
                self._clear_failed_registration()

    def list_devices(self) -> list[DiscoveredDevice]:
        cutoff = time.time() - STALE_AFTER_SECONDS
        with self._lock:
            self._devices = {
                key: device for key, device in self._devices.items() if device.last_seen >= cutoff
            }
            return sorted(self._devices.values(), key=lambda device: device.name.lower())

    def find_by_code(self, code_hash: str) -> list[DiscoveredDevice]:
        self._refresh_known_services()
        return [device for device in self.list_devices() if device.code_hash == code_hash]

    def refresh_service(self, name: str) -> None:
        with self._lifecycle_lock:
            if self._zeroconf is None or self._stop_event.is_set():
                return
            zeroconf = self._zeroconf
        with self._lock:
            self._known_services.add(name)
        with self._refresh_lock:
            try:
                info = zeroconf.get_service_info(SERVICE_TYPE, name, timeout=500)
            except Exception:
                return
        if info is None:
            return
        properties = self._properties(info.properties)
        device_id = properties.get("id", "")
        if not device_id or device_id == self._device_id:
            return
        try:
            port = int(properties["port"])
        except (KeyError, ValueError):
            return
        host = properties.get("host", "")
        if not host:
            host = self._first_service_address(info) or self._service_host(name)
        if not host:
            return
        device = DiscoveredDevice(
            id=device_id,
            name=properties.get("name", host),
            host=host,
            port=port,
            fingerprint=properties.get("fp", ""),
            code_hash=properties.get("ch", ""),
            last_seen=time.time(),
        )
        with self._lock:
            previous_id = self._service_devices.get(name)
            if previous_id and previous_id != device_id:
                self._devices.pop(previous_id, None)
            self._service_devices[name] = device_id
            self._devices[device_id] = device

    def remove_service(self, name: str) -> None:
        with self._lock:
            self._known_services.discard(name)
            device_id = self._service_devices.pop(name, "")
            if device_id:
                self._devices.pop(device_id, None)

    def _publish(self, address: str) -> None:
        with self._lifecycle_lock:
            zeroconf = self._zeroconf
            if zeroconf is None:
                return
            if self._published_service is not None:
                with suppress(Exception):
                    zeroconf.unregister_service(self._published_service)
            service = self._service_info(address)
            zeroconf.register_service(service, allow_name_change=True)
            self._published_service = service

    def _service_info(self, address: str) -> ServiceInfo:
        properties = {
            "id": self._device_id,
            "name": self._device_name,
            "host": self._host,
            "port": str(self._port),
            "fp": self._fingerprint,
            "ch": self._code_hash,
            "v": "2",
        }
        addresses = [socket.inet_aton(item) for item in self._addresses]
        if address and not self._addresses:
            addresses = [socket.inet_aton(address)]
        return ServiceInfo(
            SERVICE_TYPE,
            self._registration_name,
            addresses=addresses,
            port=self._port,
            properties=properties,
            server=f"{socket.gethostname()}.local.",
        )

    def _refresh_loop(self) -> None:
        while not self._stop_event.wait(REFRESH_INTERVAL_SECONDS):
            self._refresh_known_services()

    def _refresh_known_services(self) -> None:
        with self._lock:
            names = list(self._known_services)
        for name in names:
            self.refresh_service(name)

    def _clear_failed_registration(self) -> None:
        with self._lifecycle_lock:
            if self._browser is not None:
                with suppress(Exception):
                    self._browser.cancel()
            if self._zeroconf is not None:
                with suppress(Exception):
                    self._zeroconf.close()
            self._browser = None
            self._zeroconf = None
            self._published_service = None

    @staticmethod
    def _properties(properties: Mapping[Any, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in properties.items():
            if value is None:
                continue
            key_text = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else str(key)
            value_text = (
                value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
            )
            result[key_text] = value_text
        return result

    @staticmethod
    def _first_service_address(info: ServiceInfo) -> str:
        for address in info.addresses:
            if len(address) == 4:
                return socket.inet_ntoa(address)
        return ""

    @staticmethod
    def _service_host(name: str) -> str:
        return name.removesuffix(f".{SERVICE_TYPE}").split(".", 1)[0]

    @staticmethod
    def _safe_name(name: str) -> str:
        return (
            "".join(character for character in name if character.isalnum() or character in "-_")
            or "Relay"
        )
