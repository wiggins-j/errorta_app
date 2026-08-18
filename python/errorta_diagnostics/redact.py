"""Pure-function redaction pipeline for diagnostic bundles.

Each function takes a string and returns ``(text, count)`` where ``count`` is
the number of substitutions performed. No imports of network primitives.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Tuple

# --- Patterns ---------------------------------------------------------------

# Loopback / link-local / unspecified ranges we DO NOT redact.
_IP_KEEP = {"127.0.0.1", "0.0.0.0", "::1"}

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Any account's home prefix, not just this process's own $HOME — see
# `redact_home_like_paths`. The account segment is required (so a bare
# "/home/" or "/Users/" is left alone) and must not itself be a path
# separator.
_HOME_LIKE_RE = re.compile(r"/(?:Users|home)/[^/\s\"']+")

# Tokens we redact: OpenAI-style (sk-...), Anthropic-style (sk-ant-...),
# GitHub PAT (ghp_...), AWS access key ids (AKIA...). Each is conservatively
# length-bounded to avoid false hits.
_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{16,}|sk-ant-[A-Za-z0-9_\-]{16,}|"
    r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,})\b"
)

# SSH/SCP host disclosure. An ``ssh`` family command word anywhere in the text
# means a ``user@host`` token is an SSH target, not an email — redact it. A
# ``[user@]host:/path`` spec (scp/rsync) is redacted regardless of context
# (the trailing ``:/`` or ``:~`` never appears in an email address).
_SSH_CTX_RE = re.compile(r"\b(?:ssh|scp|sftp|rsync)\b", re.IGNORECASE)
_USER_AT_HOST_RE = re.compile(
    r"\b[A-Za-z0-9_.\-]+@[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+"
)
# Lookahead is `:~` or `:/` NOT followed by another `/`, so a URL scheme
# (`http://`, `https://`) is NOT mistaken for a `host:/path` scp spec.
_SCP_SPEC_RE = re.compile(
    r"\b(?:[A-Za-z0-9_.\-]+@)?[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*(?=:(?:~|/(?!/)))"
)


# --- Helpers ----------------------------------------------------------------


def _sub_count(pattern: re.Pattern[str], replacement: str, text: str) -> Tuple[str, int]:
    new_text, n = pattern.subn(replacement, text)
    return new_text, n


# --- Public redactors -------------------------------------------------------


def redact_home_path(text: str, home: str | None = None) -> Tuple[str, int]:
    """Replace the absolute home-directory path with the literal token ``$HOME``."""
    h = home if home is not None else os.environ.get("HOME") or str(Path.home())
    if not h:
        return text, 0
    h = h.rstrip("/")
    if not h or h not in text:
        return text, 0
    n = text.count(h)
    return text.replace(h, "$HOME"), n


def redact_home_like_paths(text: str) -> Tuple[str, int]:
    """Replace ANY user's home-directory prefix with ``$HOME``.

    ``redact_home_path`` only rewrites the path of the account this process is
    running as. That covers machine-generated diagnostics, but not free text
    **authored by a person or a model** — a North Star reading "ship it via
    /Users/example/.ssh/id", or a task titled "wire /home/alice/secrets.txt
    loader", names a directory layout and a username belonging to someone
    else entirely and still leaks when projected to a paired phone.

    Matches the POSIX shapes macOS and Linux actually use (``/Users/<name>``
    and ``/home/<name>``) and rewrites only the prefix, so the trailing path
    survives for context: ``/Users/example/.ssh/id`` -> ``$HOME/.ssh/id``.
    ``/home/`` alone (no account segment) is left untouched — it names no one.
    """
    return _sub_count(_HOME_LIKE_RE, "$HOME", text)


def redact_username(text: str, username: str | None = None) -> Tuple[str, int]:
    """Replace the local username with the literal token ``$USER``.

    Skips redaction when the username is empty or under three characters
    (too short to match safely without false positives).
    """
    u = username if username is not None else os.environ.get("USER") or ""
    if not u or len(u) < 3:
        return text, 0
    # word-bounded substitution to avoid mangling unrelated substrings.
    pattern = re.compile(rf"\b{re.escape(u)}\b")
    new_text, n = pattern.subn("$USER", text)
    return new_text, n


def redact_ips(text: str) -> Tuple[str, int]:
    """Replace non-loopback IPv4 addresses with ``<ip-redacted>``."""
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        ip = match.group(0)
        if ip in _IP_KEEP:
            return ip
        count += 1
        return "<ip-redacted>"

    new_text = _IPV4_RE.sub(_replace, text)
    return new_text, count


def redact_tokens(text: str) -> Tuple[str, int]:
    """Replace OpenAI / GitHub / AWS tokens with ``<token-redacted>``."""
    return _sub_count(_TOKEN_RE, "<token-redacted>", text)


def redact_ssh_host(text: str) -> Tuple[str, int]:
    """Replace SSH/SCP host targets with ``<ssh-host-redacted>``.

    Redacts ``[user@]host:/path`` scp/rsync specs always, and ``user@host``
    tokens when an ssh-family command word is present in the text. Ordinary
    email addresses in non-SSH log lines are left intact.
    """
    count = 0
    out, n1 = _SCP_SPEC_RE.subn("<ssh-host-redacted>", text)
    count += n1
    if _SSH_CTX_RE.search(out):
        out, n2 = _USER_AT_HOST_RE.subn("<ssh-host-redacted>", out)
        count += n2
    return out, count


def redact_corpus_paths(text: str, corpus_roots: list[str] | None = None) -> Tuple[str, int]:
    """Replace registered corpus root directories with ``<corpus-path>``.

    Corpus roots are caller-supplied. Returns the input unchanged when the
    caller passes no roots.
    """
    if not corpus_roots:
        return text, 0
    total = 0
    out = text
    for root in corpus_roots:
        r = (root or "").rstrip("/")
        if not r:
            continue
        if r in out:
            total += out.count(r)
            out = out.replace(r, "<corpus-path>")
    return out, total


# --- Pipeline ---------------------------------------------------------------


def apply_pipeline(
    text: str,
    *,
    home: str | None = None,
    username: str | None = None,
    corpus_roots: list[str] | None = None,
) -> Tuple[str, dict[str, int]]:
    """Run all redactors in order; return the redacted text + per-rule counts.

    Order is significant: home-path redaction runs first so later rules
    operate on the ``$HOME``-normalised text.
    """
    counts: dict[str, int] = {}
    text, counts["home_path"] = redact_home_path(text, home=home)
    # After the exact-$HOME pass so this process's own home still counts under
    # "home_path"; this catches OTHER accounts' home prefixes, which appear in
    # human/model-authored free text (a North Star naming /Users/example/...).
    text, counts["home_like_path"] = redact_home_like_paths(text)
    text, counts["username"] = redact_username(text, username=username)
    # SSH host before IPs so a ``user@1.2.3.4`` target redacts as a whole
    # rather than leaving a dangling ``user@<ip-redacted>``.
    text, counts["ssh_host"] = redact_ssh_host(text)
    text, counts["ips"] = redact_ips(text)
    text, counts["tokens"] = redact_tokens(text)
    text, counts["corpus_paths"] = redact_corpus_paths(text, corpus_roots=corpus_roots)
    return text, counts
