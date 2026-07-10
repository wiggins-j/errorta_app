"""Source-aware redaction pipeline for Council outbound context (F031-07).

Two stages:
1. exclude_disallowed_classes() — drops SourceEnvelopes whose class_ is
   forbidden for the destination_scope BEFORE any string scanning.
2. redact_envelopes() — applies the deterministic rule set to envelope
   .content; counts rules-fired by category; returns rewritten envelopes
   + counts. Optional post-transform scan re-checks for any sentinel
   pattern and raises RedactionLeakError if found (invariant 4 backstop).

Sharing-helpers note: this module is NOT a thin wrapper over
errorta_diagnostics.redact. Council outbound redaction is source-aware:
the destination_scope and source class_ shape what gets dropped vs
rewritten. Helpers may be lifted between the two over time, but this
module does not import errorta_diagnostics today.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from .schema import SourceEnvelope

REDACTION_VERSION = 1

_EXCLUDED_CLASSES_BY_SCOPE: dict[str, set[str]] = {
    "remote": {"raw_source_file", "raw_credentials", "raw_diagnostic_notes",
               "raw_outbound_payload", "transcript_event_beyond_cursor"},
    "local":  {"raw_credentials", "raw_outbound_payload"},
    "blocked": {"raw_source_file", "raw_credentials", "raw_diagnostic_notes",
                "raw_outbound_payload"},
    "fake":   {"raw_credentials", "raw_outbound_payload"},
}

_HOME_PATH_RE = re.compile(r"/Users/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+")
_ENV_VAR_RE = re.compile(r"\$[A-Z_][A-Z0-9_]*")
_PROVIDER_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|xoxb-[A-Za-z0-9-]{8,})\b")
_AUTH_HEADER_RE = re.compile(r"(Authorization:\s*Bearer\s+)([A-Za-z0-9._\-]+)", re.IGNORECASE)
_LOOPBACK_RE = re.compile(r"127\.0\.0\.\d{1,3}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


class RedactionLeakError(Exception):
    """Post-transform scan detected a sentinel pattern remaining."""


class RedactionPipeline:
    def __init__(
        self,
        *,
        version: int = REDACTION_VERSION,
        private_hostnames: Iterable[str] | None = None,
        _force_skip_user_var: bool = False,
    ) -> None:
        self.version = version
        self._private_hostnames = list(private_hostnames or [])
        self._force_skip_user_var = _force_skip_user_var

    def exclude_disallowed_classes(
        self,
        envelopes: list[SourceEnvelope],
        *,
        destination_scope: str,
    ) -> tuple[list[SourceEnvelope], list[dict]]:
        excluded = _EXCLUDED_CLASSES_BY_SCOPE.get(destination_scope, set())
        kept: list[SourceEnvelope] = []
        dropped: list[dict] = []
        for env in envelopes:
            if env.class_ in excluded:
                dropped.append({
                    "reason": "source_class_excluded",
                    "class_": env.class_,
                    "content_sha256": env.content_sha256,
                })
            else:
                kept.append(env)
        return kept, dropped

    def redact_envelopes(
        self,
        envelopes: list[SourceEnvelope],
        *,
        destination_scope: str,
        _enforce_scan: bool = True,
    ) -> tuple[list[SourceEnvelope], dict[str, int]]:
        counts: dict[str, int] = {
            "home_path": 0, "env_var": 0, "provider_token": 0,
            "auth_header": 0, "non_loopback_ip": 0, "private_hostname": 0,
        }
        out: list[SourceEnvelope] = []
        for env in envelopes:
            text = env.content
            text, n = _HOME_PATH_RE.subn("[REDACTED:home_path]", text)
            counts["home_path"] += n
            if not self._force_skip_user_var:
                text, n = _ENV_VAR_RE.subn("[REDACTED:env_var]", text)
                counts["env_var"] += n
            text, n = _PROVIDER_TOKEN_RE.subn("[REDACTED:provider_token]", text)
            counts["provider_token"] += n
            def _auth_sub(m):
                return m.group(1) + "[REDACTED:auth_header]"
            text, n = _AUTH_HEADER_RE.subn(_auth_sub, text)
            counts["auth_header"] += n
            # IP handling — preserve loopback; redact other IPv4s.
            def _ip_sub(m):
                if _LOOPBACK_RE.fullmatch(m.group(0)):
                    return m.group(0)
                return "[REDACTED:non_loopback_ip]"
            non_loop_hits = sum(
                1 for m in _IPV4_RE.finditer(text)
                if not _LOOPBACK_RE.fullmatch(m.group(0))
            )
            text = _IPV4_RE.sub(_ip_sub, text)
            counts["non_loopback_ip"] += non_loop_hits
            for host in self._private_hostnames:
                host_re = re.compile(r"\b" + re.escape(host) + r"(?:\.[A-Za-z0-9.-]+)?\b")
                text, n = host_re.subn("[REDACTED:private_hostname]", text)
                counts["private_hostname"] += n
            out.append(replace(env, content=text))
        if _enforce_scan:
            self._post_scan(out, destination_scope=destination_scope)
        return out, counts

    def _post_scan(self, envelopes: list[SourceEnvelope], *, destination_scope: str) -> None:
        for env in envelopes:
            if _HOME_PATH_RE.search(env.content):
                raise RedactionLeakError(f"home_path leak in {env.content_sha256[:16]}")
            if _ENV_VAR_RE.search(env.content):
                raise RedactionLeakError(f"env_var leak in {env.content_sha256[:16]}")
            if _PROVIDER_TOKEN_RE.search(env.content):
                raise RedactionLeakError(f"provider_token leak in {env.content_sha256[:16]}")


__all__ = ["RedactionPipeline", "REDACTION_VERSION", "RedactionLeakError"]
