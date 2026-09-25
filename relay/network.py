from __future__ import annotations

import hashlib
import ipaddress
import ssl
from typing import Any
from urllib.parse import urlparse

import certifi
import httpx


class PeerConnectionError(RuntimeError):
    pass


def normalize_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PeerConnectionError("That device address is not valid.")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
        if not address.is_private and not address.is_loopback and not address.is_link_local:
            raise PeerConnectionError("Relay only connects to devices on your local network.")
    except ValueError:
        if not host.endswith(".local") and host != "localhost":
            raise PeerConnectionError("Relay only connects to local network devices.") from None
    if not parsed.port:
        raise PeerConnectionError("That device address is missing a port.")
    return f"https://{host}:{parsed.port}"


def fetch_pinned_identity(
    endpoint: str, expected_fingerprint: str
) -> tuple[dict[str, Any], httpx.Client]:
    normalized = normalize_endpoint(endpoint)
    with httpx.Client(verify=False, timeout=httpx.Timeout(10.0), follow_redirects=False) as probe:
        try:
            response = probe.get(f"{normalized}/api/v1/remote/identity")
            response.raise_for_status()
            stream = response.extensions.get("network_stream")
            if stream is None:
                raise PeerConnectionError("The nearby device did not provide a secure certificate.")
            certificate = stream.get_extra_info("ssl_object").getpeercert(True)
        except (httpx.HTTPError, AttributeError, TypeError) as error:
            raise PeerConnectionError("The nearby device could not be reached.") from error
    fingerprint = ":".join(f"{byte:02X}" for byte in hashlib.sha256(certificate).digest())
    if fingerprint.upper() != expected_fingerprint.upper():
        raise PeerConnectionError("The device security fingerprint did not match. Do not continue.")
    certificate_pem = ssl.DER_cert_to_PEM_cert(certificate)
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cadata=certificate_pem)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return response.json(), httpx.Client(
        verify=context, timeout=httpx.Timeout(30.0), follow_redirects=False
    )


def open_peer_client(
    endpoint: str, expected_fingerprint: str
) -> tuple[dict[str, Any], httpx.Client]:
    return fetch_pinned_identity(endpoint, expected_fingerprint)
