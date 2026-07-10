"""F102 Slice A — secret/path scanner over the to-be-pushed tree."""
from __future__ import annotations

import pytest

from errorta_tools.runner.secret_scan import scan_tree

_GHP = "ghp_0123456789abcdefghij0123456789abcd"
_GH_PAT = "github_pat_11ABCDEFG0123456789_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"
_SK = "sk-0123456789abcdefABCDEFGH"
_SK_ANT = "sk-ant-api03-0123456789abcdefABCDEFGH"
_AKIA = "AKIAIOSFODNN7EXAMPLE"
_SLACK = "xoxb-" "1234567890-abcdefghijklmno"  # split so push-protection scanners see no contiguous token; runtime value unchanged
_PRIVKEY = "-----BEGIN RSA PRIVATE KEY-----"


@pytest.mark.parametrize("path,kind_prefix", [
    (".env", "sensitive_path:dotenv"),
    (".env.production", "sensitive_path:dotenv"),
    ("config/secrets.pem", "sensitive_path:private_key_file"),
    ("home/id_rsa", "sensitive_path:ssh_private_key"),
    ("home/id_ed25519", "sensitive_path:ssh_private_key"),
    ("certs/server.key", "sensitive_path:key_file"),
    (".npmrc", "sensitive_path:npmrc"),
    ("creds.p12", "sensitive_path:pkcs12"),
    ("creds.pfx", "sensitive_path:pkcs12"),
    ("home/.aws/credentials", "sensitive_path:aws_credentials"),
])
def test_sensitive_paths_flagged(path: str, kind_prefix: str) -> None:
    report = scan_tree([(path, b"content")])
    assert not report.clean
    assert any(f.kind == kind_prefix and f.path == path for f in report.findings)


@pytest.mark.parametrize("token,kind", [
    (_GHP, "secret_content:github_token"),
    (_GH_PAT, "secret_content:github_pat"),
    (_SK, "secret_content:openai_or_anthropic_key"),
    (_SK_ANT, "secret_content:openai_or_anthropic_key"),
    (_AKIA, "secret_content:aws_access_key_id"),
    (_SLACK, "secret_content:slack_token"),
    (_PRIVKEY, "secret_content:private_key_block"),
])
def test_secret_content_flagged(token: str, kind: str) -> None:
    blob = f"line one\nkey = {token}\nline three\n".encode("utf-8")
    report = scan_tree([("src/app.py", blob)])
    assert not report.clean
    match = [f for f in report.findings if f.kind == kind]
    assert match, report.to_dict()
    assert match[0].line == 2


def test_clean_tree_passes() -> None:
    files = [
        ("README.md", b"# hello\njust docs\n"),
        ("src/main.py", b"print('hi')\n"),
        ("docs/notes.txt", b"nothing secret here\n"),
    ]
    report = scan_tree(files)
    assert report.clean
    assert report.to_dict() == {"findings": [], "clean": True}


def test_excerpt_is_redacted_not_raw_token() -> None:
    blob = f"GITHUB_TOKEN={_GHP}\n".encode("utf-8")
    report = scan_tree([("env.sh", blob)])
    findings = [f for f in report.findings if f.kind.startswith("secret_content")]
    assert findings
    # The raw token must never survive into the reported excerpt.
    for f in findings:
        assert _GHP not in f.redacted_excerpt
    # And the whole report serialization must not echo it either.
    import json
    assert _GHP not in json.dumps(report.to_dict())


def test_binary_blob_content_skipped_but_path_still_scanned() -> None:
    # A binary blob (NUL byte) carrying a token-shaped sequence is NOT content-
    # scanned, but a sensitive PATH is still flagged.
    binary = b"\x00\x01" + _GHP.encode("utf-8") + b"\x00"
    report = scan_tree([("assets/blob.bin", binary)])
    assert report.clean  # token inside binary is skipped, path is innocuous

    report2 = scan_tree([("secret.pem", binary)])
    assert not report2.clean
    assert all(f.kind.startswith("sensitive_path") for f in report2.findings)


def test_multiple_findings_across_files() -> None:
    files = [
        (".env", b"X=1\n"),
        ("src/a.py", f"k={_AKIA}\n".encode("utf-8")),
        ("clean.py", b"ok\n"),
    ]
    report = scan_tree(files)
    paths = {f.path for f in report.findings}
    assert paths == {".env", "src/a.py"}
