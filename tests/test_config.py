from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from relay.config import load_or_create_identity


def test_identity_renews_certificate_after_ip_change(tmp_path: Path) -> None:
    original = load_or_create_identity(tmp_path, "PC", ["127.0.0.1", "192.168.1.10"], 8765)
    key = (tmp_path / "identity.key").read_bytes()

    renewed = load_or_create_identity(tmp_path, "PC", ["127.0.0.1", "172.17.26.226"], 8765)

    cert = x509.load_pem_x509_certificate((tmp_path / "identity.crt").read_bytes())
    addresses = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("172.17.26.226") in addresses.get_values_for_type(x509.IPAddress)
    assert renewed.id == original.id
    assert renewed.fingerprint != original.fingerprint
    assert (tmp_path / "identity.key").read_bytes() == key


def test_identity_keeps_valid_certificate_for_existing_addresses(tmp_path: Path) -> None:
    original = load_or_create_identity(tmp_path, "PC", ["127.0.0.1", "192.168.1.10"], 8765)
    certificate = (tmp_path / "identity.crt").read_bytes()

    for addresses in [["192.168.1.10", "127.0.0.1"], ["127.0.0.1"]]:
        loaded = load_or_create_identity(tmp_path, "Renamed PC", addresses, 8766)
        assert loaded.id == original.id
        assert loaded.fingerprint == original.fingerprint
        assert (tmp_path / "identity.crt").read_bytes() == certificate


def test_identity_renews_expired_certificate(tmp_path: Path) -> None:
    original = load_or_create_identity(tmp_path, "PC", ["127.0.0.1"], 8765)
    key_bytes = (tmp_path / "identity.key").read_bytes()
    key = serialization.load_pem_private_key(key_bytes, password=None)
    cert_path = tmp_path / "identity.crt"
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(cert.subject)
        .issuer_name(cert.issuer)
        .public_key(cert.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1))
    )
    for extension in cert.extensions:
        builder = builder.add_extension(extension.value, extension.critical)
    expired = builder.sign(key, hashes.SHA256())
    cert_path.write_bytes(expired.public_bytes(serialization.Encoding.PEM))

    renewed = load_or_create_identity(tmp_path, "PC", ["127.0.0.1"], 8765)

    assert renewed.id == original.id
    assert (tmp_path / "identity.key").read_bytes() == key_bytes
    assert x509.load_pem_x509_certificate(cert_path.read_bytes()).not_valid_after_utc > now
