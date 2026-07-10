from __future__ import annotations

import json
import zipfile
from pathlib import Path

from errorta_diagnostics import build_bundle


def test_diagnostics_redacts_gateway_settings_and_summarizes_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    gateway_dir = home / "model-gateway"
    gateway_dir.mkdir(parents=True)
    (gateway_dir / "policy.json").write_text(
        json.dumps(
            {
                "global_mode": "you_pick",
                "provider": {"api_key": "sk-ant-secretsecretsecretsecret"},
                "corpus_policies": {"welcome": "redacted_support"},
            }
        ),
        encoding="utf-8",
    )
    (gateway_dir / "audit.jsonl").write_text(
        json.dumps(
            {
                "request_id": "gw_1",
                "role": "judge",
                "provider": "anthropic",
                "preview_redacted": "prompt text that should not be exported",
                "payload_sha256": "abc",
                "status": "ok",
                "tokens": {"input": 1, "output": 2},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    dest = tmp_path / "diagnostics.zip"
    build_bundle(dest)

    with zipfile.ZipFile(dest) as zf:
        settings = json.loads(zf.read("model-gateway-settings.json"))
        audit = json.loads(zf.read("model-gateway-audit-summary.json"))

    assert settings["provider"]["api_key"] == "<redacted>"
    assert audit[0]["request_id"] == "gw_1"
    assert audit[0]["payload_sha256"] == "abc"
    assert "preview_redacted" not in audit[0]
