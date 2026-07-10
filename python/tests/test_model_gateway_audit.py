from __future__ import annotations

from pathlib import Path

from errorta_model_gateway import audit


def test_audit_event_hashes_payload_and_redacts_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    payload = "ask help@errorta.app using sk-ant-secretsecretsecret"

    event = audit.append(
        audit.build_event(
            status="blocked_by_policy",
            role="judge",
            provider="anthropic",
            model="claude",
            corpus="welcome",
            egress_policy="redacted_support",
            egress_class="answer_plus_redacted_snippets",
            payload_fields=["prompt", "answer", "redacted_snippets"],
            payload_hash=audit.payload_sha256(payload),
            preview_redacted=audit.preview_text(payload),
            blocked_reason="blocked",
        )
    )

    [record] = audit.list_events()
    assert record["request_id"] == event.id
    assert record["payload_sha256"]
    assert "sk-ant-secret" not in record["preview_redacted"]
    assert "<token-redacted>" in record["preview_redacted"]
    assert "help@errorta.app" not in record["preview_redacted"]
    assert "<email-redacted>" in record["preview_redacted"]


def test_list_events_skips_malformed_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    path = tmp_path / "model-gateway" / "audit.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"request_id": "a"}\nnot-json\n{"request_id": "b"}\n',
        encoding="utf-8",
    )

    records = audit.list_events()

    assert [r["request_id"] for r in records] == ["a", "b"]
