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


# --- foreign home paths (not this process's own $HOME) ----------------------


def test_redact_home_like_paths_covers_other_accounts() -> None:
    """`redact_home_path` only rewrites the path of the account this process
    runs as. Free text authored by a person or a model routinely names someone
    ELSE's home — that still leaks a username and a directory layout."""
    out, n = redact.redact_home_like_paths("ship it via /Users/example/.ssh/id")
    assert out == "ship it via $HOME/.ssh/id"
    assert n == 1

    out2, n2 = redact.redact_home_like_paths("wire /home/alice/secrets.txt")
    assert out2 == "wire $HOME/secrets.txt"
    assert n2 == 1


def test_redact_home_like_paths_leaves_accountless_prefixes_alone() -> None:
    """A bare `/home/` or `/Users/` names nobody — redacting it would mangle
    prose without protecting anyone."""
    for text in ("see /home/ for details", "under /Users/ somewhere"):
        out, n = redact.redact_home_like_paths(text)
        assert out == text and n == 0


def test_apply_pipeline_redacts_a_foreign_home_path() -> None:
    out, counts = redact.apply_pipeline("ship it via /Users/example/.ssh/id")
    assert "/Users/example" not in out
    assert counts.get("home_like_path", 0) == 1


def test_home_like_rule_does_not_defeat_corpus_redaction() -> None:
    """Ordering lock: the home-like rule must run AFTER corpus redaction.

    Run earlier it rewrote the home prefix a corpus root starts with, so the
    literal root no longer matched and the corpus NAME leaked —
    "$HOME/legal-docs/case.pdf" instead of "<corpus-path>/case.pdf".
    """
    out, counts = redact.apply_pipeline(
        "opened /home/shared/legal-docs/case-1234.pdf",
        corpus_roots=["/home/shared/legal-docs"],
    )
    assert out == "opened <corpus-path>/case-1234.pdf"
    assert "legal-docs" not in out
    assert counts.get("corpus_paths", 0) == 1


def test_home_like_rule_leaves_url_path_segments_alone() -> None:
    """A /home/... segment inside a URL is a route, not a home directory.

    Errorta previews user web projects and redacts the log tail, so a project
    with a /home route is ordinary; rewriting it to $HOME destroyed the request
    path an operator is reading.
    """
    line = "GET http://localhost:3000/home/dashboard 200"
    out, _ = redact.apply_pipeline(line)
    assert out == line

    # ...but a file:// URL genuinely names a home directory and must redact.
    out2, _ = redact.apply_pipeline("open file:///Users/carol/x.txt")
    assert "/Users/carol" not in out2


def test_home_like_rule_keeps_trailing_punctuation() -> None:
    out, _ = redact.apply_pipeline("see (/Users/bob) now")
    assert out == "see ($HOME) now"


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
