"""F065 — self-signed TLS for the mobile LAN listener.

The phone↔desktop channel must be TLS, not plaintext on the LAN. We generate a
self-signed leaf the iOS app pins by its DER SHA-256 fingerprint. The cert
carries a SAN matching the bind host (an IP SAN for an IP bind), which iOS
requires regardless of pinning.

The fingerprint is the SHA-256 of the certificate's **DER** encoding — the same
bytes iOS sees from ``SecCertificateCopyData`` — so the pin matches on both
ends. (Hashing the PEM file bytes would never match.)
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import os
from collections.abc import Sequence
from pathlib import Path

CERT_NAME = "server-cert.pem"
KEY_NAME = "server-key.pem"


def _san_for_host(host: str):
    from cryptography import x509

    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def _normalize_hosts(hosts: str | Sequence[str]) -> list[str]:
    items = [hosts] if isinstance(hosts, str) else list(hosts)
    out: list[str] = []
    for raw in items:
        h = str(raw).strip()
        if h and h not in out:
            out.append(h)
    return out


def ensure_self_signed(
    hosts: str | Sequence[str], tls_dir: str | Path, *, rotate: bool = False
) -> tuple[Path, Path]:
    """Idempotently ensure a self-signed cert+key exist (0600). ``hosts`` may be
    a single host or a list; the SAN covers them all on first generation.

    F076 — the cert is **stable**: once it exists it is REUSED, never regenerated
    just because the bind set grew (e.g. Tailscale enabled later). The iOS client
    pins the leaf's DER SHA-256 and bypasses hostname/SAN validation, so the same
    cert is accepted over a Tailscale host whose IP isn't in the SAN. Keeping the
    fingerprint stable means a phone pairs ONCE and roams LAN↔Tailscale without
    ever re-pairing. Pass ``rotate=True`` to force a fresh cert (explicit
    rotation — invalidates existing pairings, which must re-pair).

    Returns (cert_path, key_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    host_list = _normalize_hosts(hosts)
    if not host_list:
        raise ValueError("ensure_self_signed requires at least one host")

    tls_dir = Path(tls_dir)
    tls_dir.mkdir(parents=True, exist_ok=True)
    cert_path = tls_dir / CERT_NAME
    key_path = tls_dir / KEY_NAME

    if not rotate and cert_path.exists() and key_path.exists():
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host_list[0])])
    # Fixed epoch start (Date.now is unavailable in some sandboxes; use utcnow
    # here — this is the sidecar process, not a workflow script).
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=825))  # iOS max leaf lifetime
        .add_extension(
            x509.SubjectAlternativeName([_san_for_host(h) for h in host_list]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    # Write key first (0600 via opener), then cert.
    _write_0600(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    _write_0600(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def cert_der_sha256(cert_path: str | Path) -> str:
    """SHA-256 of the cert's DER encoding (the bytes iOS pins). Hex string."""
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    from cryptography.hazmat.primitives import serialization

    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def _cert_covers_host(cert_path: Path, host: str) -> bool:
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except Exception:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        return host in san.get_values_for_type(x509.DNSName)


def _write_0600(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


__all__ = ["CERT_NAME", "KEY_NAME", "cert_der_sha256", "ensure_self_signed"]
