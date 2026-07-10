"""F102 — secret/path scanner for the publish flow (regex + sensitive paths).

Pure, no egress: the caller hands over the to-be-pushed TREE as a list of
``(rel_path, raw_bytes)`` (e.g. extracted from ``git archive`` / ``export_master``
tracked files) and this module flags sensitive file PATHS and high-confidence
secret CONTENT regexes. RC7: scan the whole tree, not the diff, so a pre-existing
secret in an unchanged file is still caught (esp. the P4 new-repo path).

Fail-closed posture (D-OQ2): a tight, unit-tested regex set + sensitive paths;
no gitleaks dependency. Findings are reported per-file; the caller blocks the
push (409) unless the operator passes an explicit override. Every excerpt is
redacted via :mod:`errorta_diagnostics.redact` before it leaves this module, so a
matched token never lands verbatim in an event/response.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from errorta_diagnostics.redact import (
    redact_home_path,
    redact_tokens,
    redact_username,
)

# Sensitive PATHS (matched on the basename or a path suffix). fnmatch globs.
_SENSITIVE_PATH_GLOBS: tuple[tuple[str, str], ...] = (
    (".env", "dotenv"),
    (".env.*", "dotenv"),
    ("*.pem", "private_key_file"),
    ("id_rsa", "ssh_private_key"),
    ("id_dsa", "ssh_private_key"),
    ("id_ecdsa", "ssh_private_key"),
    ("id_ed25519", "ssh_private_key"),
    ("*.key", "key_file"),
    (".npmrc", "npmrc"),
    ("*.p12", "pkcs12"),
    ("*.pfx", "pkcs12"),
)

# Sensitive path SUFFIXES (a full relative-path tail, e.g. ".aws/credentials").
_SENSITIVE_PATH_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".aws/credentials", "aws_credentials"),
)

# High-confidence secret CONTENT patterns.
_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    # sk-ant-... must be tried as the more specific Anthropic shape, but a bare
    # sk- secret is also flagged; one combined pattern covers both.
    ("openai_or_anthropic_key", re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
)

# A blob with a NUL byte is treated as binary and skipped for content scanning
# (path rules still apply). Mirrors the apply_workspace binary heuristic.
_NUL = b"\x00"


def _redact_excerpt(text: str) -> str:
    text, _ = redact_tokens(text)
    text, _ = redact_home_path(text)
    text, _ = redact_username(text)
    return text


@dataclass(frozen=True)
class ScanFinding:
    path: str
    kind: str
    line: int | None = None
    redacted_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "line": self.line,
            "redacted_excerpt": self.redacted_excerpt,
        }


@dataclass(frozen=True)
class ScanReport:
    findings: list[ScanFinding] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "clean": self.clean,
        }


def _path_findings(rel_path: str) -> list[ScanFinding]:
    basename = rel_path.rsplit("/", 1)[-1]
    normalized = rel_path.replace("\\", "/")
    out: list[ScanFinding] = []
    for glob, kind in _SENSITIVE_PATH_GLOBS:
        if fnmatch.fnmatch(basename, glob):
            out.append(ScanFinding(path=rel_path, kind=f"sensitive_path:{kind}"))
            break
    for suffix, kind in _SENSITIVE_PATH_SUFFIXES:
        if normalized == suffix or normalized.endswith("/" + suffix):
            out.append(ScanFinding(path=rel_path, kind=f"sensitive_path:{kind}"))
            break
    return out


def _content_findings(rel_path: str, blob: bytes) -> list[ScanFinding]:
    if _NUL in blob:
        return []  # binary — skip content scan (RC: skip binary blobs)
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode with errors never raises
        return []
    out: list[ScanFinding] = []
    seen: set[tuple[str, int]] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _CONTENT_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue
            key = (kind, line_no)
            if key in seen:
                continue
            seen.add(key)
            # Bound the excerpt then redact — never emit the raw matched secret.
            excerpt = line.strip()[:200]
            out.append(ScanFinding(
                path=rel_path, kind=f"secret_content:{kind}", line=line_no,
                redacted_excerpt=_redact_excerpt(excerpt)))
    return out


def scan_tree(files: list[tuple[str, bytes]]) -> ScanReport:
    """Scan the to-be-pushed tree for sensitive paths + secret content.

    ``files`` is ``[(rel_path, raw_bytes), ...]`` for every tracked file in the
    tree being published. Returns a :class:`ScanReport`; ``clean`` is True only
    when there are zero findings. Pure (no subprocess / no network)."""
    findings: list[ScanFinding] = []
    for rel_path, blob in files:
        rel_path = str(rel_path)
        findings.extend(_path_findings(rel_path))
        findings.extend(_content_findings(rel_path, blob or b""))
    return ScanReport(findings=findings)


__all__ = ["ScanFinding", "ScanReport", "scan_tree"]
