"""Ed25519 license token: encode (server/harness) + verify (sidecar).

Token wire format (spec §5): ``base64url(payload_json) + "." + base64url(sig)``
with no padding. The signature is over the **exact base64url payload string
bytes**, not a re-serialized JSON blob, so verification is canonical and needs
no JSON-canonicalization agreement between the Worker (TS) and the sidecar
(Python).

The sidecar only ever *verifies*; the private key lives solely as a Cloudflare
Worker secret. ``encode`` is provided here because the test suite and the
deterministic harness must mint tokens with an ephemeral key — production signing
is reimplemented in the Worker.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encode(payload: dict[str, Any], private_key: Any) -> str:
    """Sign ``payload`` with an ``Ed25519PrivateKey`` and return the token.

    Test/harness helper — production signing happens in the Worker.
    """
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    sig = private_key.sign(payload_b64.encode("ascii"))
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify(token: str, public_key_raw: bytes) -> dict[str, Any] | None:
    """Verify ``token`` against the 32-byte raw Ed25519 public key.

    Returns the decoded payload dict on success, or ``None`` on *any* failure
    (malformed token, bad signature, non-object payload). A ``None`` result is
    treated by the state machine as UNACTIVATED — a forged or tampered token can
    never unlock the app.
    """
    # Lazy import so merely importing this module never pulls cryptography into
    # a frozen build that isn't using the alpha gate.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        sig = _b64url_decode(sig_b64)
        public_key = Ed25519PublicKey.from_public_bytes(public_key_raw)
        public_key.verify(sig, payload_b64.encode("ascii"))
        payload = json.loads(_b64url_decode(payload_b64))
    except (InvalidSignature, ValueError, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001
        log.debug("alpha: token verification failed: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return payload
