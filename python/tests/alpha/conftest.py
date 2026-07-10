"""Fixtures for the F-DIST-01 alpha licensing suite.

Provides an isolated ``ERRORTA_HOME``, an on-by-default alpha gate, and an
ephemeral Ed25519 keypair whose public half is injected via
``ERRORTA_ALPHA_PUBKEY`` so the sidecar verifies test-minted tokens against a
key we control (never the placeholder build constant).
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from errorta_alpha import token as token_mod


@dataclass
class AlphaKeys:
    private_key: Ed25519PrivateKey
    public_b64: str

    def mint(
        self,
        *,
        device_id: str,
        code: str = "ERRT-TEST-0001",
        grace_until: int | None = None,
        issued_at: int | None = None,
        program: str = "alpha",
    ) -> str:
        now = int(time.time())
        payload = {
            "v": 1,
            "device_id": device_id,
            "code": code,
            "issued_at": issued_at if issued_at is not None else now,
            "grace_until": grace_until if grace_until is not None else now + 14 * 86400,
            "program": program,
            "build_channel": "alpha",
        }
        return token_mod.encode(payload, self.private_key)


@pytest.fixture
def alpha_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / ".errorta"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ERRORTA_HOME", str(home))
    monkeypatch.setenv("ERRORTA_ALPHA_GATE", "1")
    return home


@pytest.fixture
def alpha_keys(monkeypatch: pytest.MonkeyPatch) -> AlphaKeys:
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    pub_b64 = base64.b64encode(raw_pub).decode("ascii")
    monkeypatch.setenv("ERRORTA_ALPHA_PUBKEY", pub_b64)
    return AlphaKeys(private_key=priv, public_b64=pub_b64)
