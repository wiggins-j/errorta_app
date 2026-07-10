"""Source-aware text redaction over SourceEnvelopes.

Asserts: every sentinel category is redacted; rule counts are reported;
neither the artifact nor the manifest preview leaks any sentinel.
"""
from __future__ import annotations

import hashlib

import pytest

from errorta_council.context.transforms.redaction import (
    REDACTION_VERSION,
    RedactionLeakError,
    RedactionPipeline,
)
from errorta_council.context.transforms.schema import SourceEnvelope


def _env(content: str, *, class_="retrieved_snippet", sensitivity="known_local"):
    return SourceEnvelope(
        class_=class_, corpus_id="c1", chunk_id="ch1", citation_id="ct1",
        content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        tokens=len(content.split()), sensitivity=sensitivity)


def test_home_path_redacted():
    pipe = RedactionPipeline(version=REDACTION_VERSION, private_hostnames=["aiar"])
    env = _env("see /Users/example/secrets/aerospace.pdf for details")
    out, counts = pipe.redact_envelopes([env], destination_scope="local")
    assert "/Users/example" not in out[0].content
    assert "[REDACTED:home_path]" in out[0].content
    assert counts.get("home_path", 0) >= 1


def test_user_var_redacted():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    env = _env("logged in as $USER on machine $HOSTNAME")
    out, counts = pipe.redact_envelopes([env], destination_scope="local")
    assert "$USER" not in out[0].content
    assert counts.get("env_var", 0) >= 1


def test_provider_token_redacted():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    env = _env("Authorization: Bearer sk-proj-ABCDEFG1234567890ABCDEFG")
    out, counts = pipe.redact_envelopes([env], destination_scope="remote")
    assert "sk-proj-ABCDEFG1234567890ABCDEFG" not in out[0].content
    assert counts.get("auth_header", 0) >= 1 or counts.get("provider_token", 0) >= 1


def test_non_loopback_ip_redacted():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    env = _env("called http://192.0.2.79:8089/api with result OK")
    out, counts = pipe.redact_envelopes([env], destination_scope="remote")
    assert "192.0.2.79" not in out[0].content
    assert counts.get("non_loopback_ip", 0) >= 1


def test_loopback_ip_preserved():
    pipe = RedactionPipeline(version=REDACTION_VERSION)
    env = _env("called http://127.0.0.1:8770/healthz with result OK")
    out, counts = pipe.redact_envelopes([env], destination_scope="local")
    assert "127.0.0.1" in out[0].content
    assert counts.get("non_loopback_ip", 0) == 0


def test_private_hostname_redacted():
    pipe = RedactionPipeline(version=REDACTION_VERSION, private_hostnames=["aiar", "watchdog"])
    env = _env("ssh aiar.local 'tail -f /var/log/syslog'")
    out, counts = pipe.redact_envelopes([env], destination_scope="remote")
    assert "aiar.local" not in out[0].content
    assert counts.get("private_hostname", 0) >= 1


def test_post_transform_scan_catches_leak():
    """If a rule accidentally leaves a sentinel, the post-scan raises."""
    pipe = RedactionPipeline(version=REDACTION_VERSION, _force_skip_user_var=True)
    env = _env("hello $USER from /Users/example")
    with pytest.raises(RedactionLeakError):
        pipe.redact_envelopes([env], destination_scope="remote", _enforce_scan=True)
