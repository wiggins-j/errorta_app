from __future__ import annotations

from errorta_alpha import config


def test_frozen_gate_uses_build_stamp_not_runtime_env(monkeypatch):
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setenv("ERRORTA_ALPHA_GATE", "0")
    monkeypatch.setattr(config, "_bundled_build_info",
                        lambda: {"alpha_gate_enabled": True})
    assert config.gate_enabled() is True


def test_frozen_public_key_ignores_runtime_override(monkeypatch):
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setenv("ERRORTA_ALPHA_PUBKEY", "attacker-controlled")
    assert config.license_public_key_b64() == config.LICENSE_PUBKEY_B64
