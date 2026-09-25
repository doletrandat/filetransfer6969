from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(frozen=True, slots=True)
class Settings:
    destination: Path


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "settings.json"
        default_destination = Path.home() / "Downloads" / "Relay"
        self._settings = Settings(destination=default_destination)

    def load(self) -> Settings:
        if self._path.exists():
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            destination = Path(payload.get("destination", self._settings.destination)).expanduser()
            self._settings = Settings(destination=destination.resolve())
        self._settings.destination.mkdir(parents=True, exist_ok=True)
        return self._settings

    def update(self, destination: str) -> Settings:
        path = Path(destination).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        self._settings = Settings(destination=path)
        self._path.write_text(
            json.dumps({"destination": str(path)}, indent=2),
            encoding="utf-8",
        )
        return self._settings


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    id: str
    name: str
    fingerprint: str


def get_local_addresses() -> list[str]:
    addresses: set[str] = {"127.0.0.1"}
    route_address = ""
    hostname = socket.gethostname()
    try:
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = str(item[4][0])
            if not address.startswith("127."):
                addresses.add(address)
    except socket.gaierror:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            route_address = str(probe.getsockname()[0])
            if not route_address.startswith("127."):
                addresses.add(route_address)
    except OSError:
        pass
    ordered = [address for address in (route_address, "127.0.0.1") if address]
    ordered.extend(sorted(address for address in addresses if address not in ordered))
    return ordered


def create_device_id() -> str:
    return hashlib.sha256(f"{socket.gethostname()}:{uuid.uuid4()}".encode()).hexdigest()[:16]


def load_or_create_identity(
    data_dir: Path,
    name: str,
    addresses: list[str],
    port: int,
) -> DeviceIdentity:
    key_path = data_dir / "identity.key"
    cert_path = data_dir / "identity.crt"
    device_id_path = data_dir / "device-id"
    if device_id_path.exists():
        device_id = device_id_path.read_text(encoding="utf-8").strip()
    else:
        device_id = create_device_id()
        device_id_path.write_text(device_id, encoding="utf-8")
    if key_path.exists() and cert_path.exists():
        return DeviceIdentity(
            id=device_id, name=name, fingerprint=certificate_fingerprint(cert_path)
        )
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Relay device"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, device_id),
        ]
    )
    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName(f"{socket.gethostname()}.local"),
    ]
    sans.extend(x509.IPAddress(ipaddress.ip_address(address)) for address in addresses)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return DeviceIdentity(id=device_id, name=name, fingerprint=certificate_fingerprint(cert_path))


def certificate_fingerprint(path: Path) -> str:
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    digest = certificate.fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in digest)
