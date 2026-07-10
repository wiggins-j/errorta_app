"""F065 slice B1 — self-signed TLS cert generation + DER fingerprint."""
from __future__ import annotations

import hashlib
import ssl
import stat

from errorta_mobile import tls


def test_generates_cert_and_key_0600_with_ip_san(tmp_path):
    cert, key = tls.ensure_self_signed("192.0.2.42", tmp_path)
    assert cert.exists() and key.exists()
    # Both files are owner-only.
    assert stat.S_IMODE(cert.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # The SAN covers the IP (iOS requires a matching SAN).
    from cryptography import x509
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    import ipaddress
    assert ipaddress.ip_address("192.0.2.42") in san.get_values_for_type(x509.IPAddress)


def test_dns_san_for_hostname(tmp_path):
    cert, _ = tls.ensure_self_signed("mac.local", tmp_path)
    from cryptography import x509
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "mac.local" in san.get_values_for_type(x509.DNSName)


def test_fingerprint_is_der_sha256_not_pem(tmp_path):
    cert, _ = tls.ensure_self_signed("192.0.2.42", tmp_path)
    fp = tls.cert_der_sha256(cert)
    # It must be the DER hash (what iOS SecCertificateCopyData yields), NOT the
    # hash of the PEM file bytes.
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    der = parsed.public_bytes(serialization.Encoding.DER)
    assert fp == hashlib.sha256(der).hexdigest()
    assert fp != hashlib.sha256(cert.read_bytes()).hexdigest()  # not the PEM hash


def test_idempotent_when_host_matches(tmp_path):
    c1, k1 = tls.ensure_self_signed("192.0.2.42", tmp_path)
    fp1 = tls.cert_der_sha256(c1)
    c2, k2 = tls.ensure_self_signed("192.0.2.42", tmp_path)
    # Same files, unchanged cert (not regenerated).
    assert (c1, k1) == (c2, k2)
    assert tls.cert_der_sha256(c2) == fp1


def test_cert_is_stable_across_host_changes(tmp_path):
    # F076 — the fingerprint MUST stay stable when the bind set changes (e.g.
    # Tailscale enabled later), so a paired phone never has to re-pair. The pin
    # is by leaf DER SHA-256 and ignores the SAN, so the reused cert still works
    # over a new host.
    c1, _ = tls.ensure_self_signed("192.0.2.42", tmp_path)
    fp1 = tls.cert_der_sha256(c1)
    c2, _ = tls.ensure_self_signed(["192.0.2.42", "100.64.1.2"], tmp_path)
    assert tls.cert_der_sha256(c2) == fp1  # reused — stable pin


def test_rotate_forces_a_new_cert(tmp_path):
    c1, _ = tls.ensure_self_signed("192.0.2.42", tmp_path)
    fp1 = tls.cert_der_sha256(c1)
    c2, _ = tls.ensure_self_signed("192.0.2.42", tmp_path, rotate=True)
    assert tls.cert_der_sha256(c2) != fp1  # explicit rotation invalidates pairings


def test_cert_is_loadable_by_ssl(tmp_path):
    # uvicorn will hand these to an SSLContext — make sure they load.
    cert, key = tls.ensure_self_signed("127.0.0.1", tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
