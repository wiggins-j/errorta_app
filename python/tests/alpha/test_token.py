"""Ed25519 token encode/verify — round trip, tamper, wrong key, malformed."""
from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from errorta_alpha import token as token_mod


def _raw_pub(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def test_round_trip():
    priv = Ed25519PrivateKey.generate()
    payload = {"v": 1, "device_id": "abc", "grace_until": 1751328000}
    tok = token_mod.encode(payload, priv)
    out = token_mod.verify(tok, _raw_pub(priv))
    assert out == payload


def test_tampered_payload_fails():
    priv = Ed25519PrivateKey.generate()
    tok = token_mod.encode({"grace_until": 1}, priv)
    payload_b64, sig = tok.split(".", 1)
    # Flip a payload byte -> signature no longer matches.
    forged = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B") + "." + sig
    assert token_mod.verify(forged, _raw_pub(priv)) is None


def test_tampered_signature_fails():
    priv = Ed25519PrivateKey.generate()
    tok = token_mod.encode({"grace_until": 1}, priv)
    payload_b64, sig = tok.split(".", 1)
    # Flip a byte in the MIDDLE of the signature (the final base64 char carries
    # redundant bits and can decode to the same bytes — a real byte change needs
    # a non-terminal char).
    i = len(sig) // 2
    forged = payload_b64 + "." + sig[:i] + ("A" if sig[i] != "A" else "B") + sig[i + 1:]
    assert token_mod.verify(forged, _raw_pub(priv)) is None


def test_wrong_key_fails():
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    tok = token_mod.encode({"grace_until": 1}, priv)
    assert token_mod.verify(tok, _raw_pub(other)) is None


def test_malformed_tokens_return_none():
    priv = Ed25519PrivateKey.generate()
    pub = _raw_pub(priv)
    for bad in ["", "nodot", "a.b.c", "!!!.###", 123]:  # type: ignore[list-item]
        assert token_mod.verify(bad, pub) is None
