"""F086 Slice B — live log-tail / log-stream redaction.

The live diagnostics surfaces previously returned RAW log lines (tokens, home
paths, SSH hosts). These tests lock the new SSH-host rule and assert the tail
endpoint redacts before responding.
"""
from __future__ import annotations

import logging
import os

from fastapi.testclient import TestClient

from errorta_app.server import app
from errorta_diagnostics import redact


# --- redact_ssh_host unit ---------------------------------------------------


def test_redact_ssh_host_redacts_ssh_target() -> None:
    out, n = redact.redact_ssh_host("ssh -o BatchMode=yes you@host.example.com")
    assert "you@host.example.com" not in out
    assert "<ssh-host-redacted>" in out and n >= 1


def test_redact_ssh_host_redacts_scp_spec() -> None:
    out, n = redact.redact_ssh_host("scp -i k file user@server.lan:/var/data")
    assert "user@server.lan" not in out and n >= 1


def test_redact_ssh_host_leaves_plain_email() -> None:
    # No ssh-family word and no scp host:path spec — an email in prose stays.
    out, n = redact.redact_ssh_host("Contact help@errorta.app for support")
    assert out == "Contact help@errorta.app for support" and n == 0


def test_redact_ssh_host_leaves_url_scheme() -> None:
    # A URL scheme (http://, https://) must NOT be mistaken for a host:/path
    # scp spec — regression for over-redacting `http` in an Ollama log line.
    line = 'GET http://127.0.0.1:11434/api/tags "200 OK"'
    out, n = redact.redact_ssh_host(line)
    assert "<ssh-host-redacted>://" not in out
    assert "http://127.0.0.1:11434" in out and n == 0
    out2, _ = redact.redact_ssh_host("fetched https://example.com/x")
    assert "https://example.com" in out2


def test_apply_pipeline_includes_ssh_host() -> None:
    out, counts = redact.apply_pipeline("ssh deploy@prod.internal echo hi")
    assert "deploy@prod.internal" not in out
    assert counts.get("ssh_host", 0) >= 1


# --- route: tail redacts ----------------------------------------------------


def test_log_tail_redacts_secrets(tmp_errorta_home) -> None:
    logger = logging.getLogger("f086.redact.tail")
    home = os.environ.get("HOME") or ""
    token = "sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"

    with TestClient(app) as client:
        app.state.log_buffer.clear()
        logger.warning("token=%s", token)
        logger.warning("path=%s/.errorta/secret", home)
        logger.warning("tunnel via ssh deploy@host.example.net up")
        logger.warning("benign email help@errorta.app noted")

        resp = client.get("/diagnostics/log-tail?lines=10")

    assert resp.status_code == 200
    body = "\n".join(resp.json()["lines"])
    assert token not in body
    assert "deploy@host.example.net" not in body
    if home and len(home) > 1:
        assert f"{home}/.errorta/secret" not in body
    # email in a non-ssh line is preserved
    assert "help@errorta.app" in body
