"""F120 — member health classification (pure functions, no egress).

When a Coding Team member cannot actually produce output — a logged-out
``claude_cli``, a missing ``codex`` binary, an expired key, a provider 401/429,
a model that never emits parseable intent — the underlying error must be turned
into a *typed* outcome instead of being swallowed by the loop's failure-isolation
wrapper. This module owns that classification.

It is deliberately dependency-free of the attention policy and the loop: it maps
an exception (or message) into a :class:`MemberFailure` ``{status, detail,
remediation}`` and computes a classify-aware cap. The loop (``autonomy.py``)
counts consecutive failures and raises the blocking Problem; the runner
(``runner.py``) surfaces the failure through ``TurnOutcome``. Keeping this pure
makes "this can't recur" test-lockable against the *real* gateway exception
strings (criterion #1).

Every ``detail`` string is run through ``errorta_diagnostics.redact`` so no
credential or raw key can leak into a signal/log (criterion #7), even though the
gateway already redacts at its own boundary — defense-in-depth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .autonomy import CodingAutonomyPolicy

# Outcome status enum (spec acceptance criterion #1).
OK = "ok"
AUTH_FAILED = "auth_failed"
BINARY_MISSING = "binary_missing"
MODEL_REJECTED = "model_rejected"
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
UNPARSEABLE = "unparseable"
ERRORED = "errored"

STATUSES = (
    OK, AUTH_FAILED, BINARY_MISSING, MODEL_REJECTED, TIMEOUT, RATE_LIMITED,
    UNPARSEABLE, ERRORED,
)

# Reasons the loop must treat as TERMINAL — a logged-out CLI, a missing binary,
# or a model the provider no longer offers will not heal by retrying, so they
# cap at a single failure. (A renamed/removed model — e.g. Cursor dropping
# `gpt-5` for the `gpt-5.3-codex` family — fails identically every attempt.)
_TERMINAL_STATUSES = frozenset({AUTH_FAILED, BINARY_MISSING, MODEL_REJECTED})

# One-step remediation per reason. Keyed off the classified status so setup-time
# (Test), pre-run (preflight) and mid-run (Problem) wording stay consistent.
_REMEDIATION: dict[str, str] = {
    AUTH_FAILED: "Run the login command for this provider in Settings → Providers, then retry.",
    BINARY_MISSING: "Install or locate the CLI binary in Settings → Providers, then retry.",
    MODEL_REJECTED: (
        "The provider no longer offers this model. Edit this member in the room "
        "editor and pick a valid model (or the account default), then retry."
    ),
    TIMEOUT: "The provider timed out. Check the network/provider and retry.",
    RATE_LIMITED: "The provider is rate-limited. Wait and retry, or use a different model.",
    UNPARSEABLE: "The model returned no usable output. Try a different model for this member.",
    ERRORED: "The provider call failed. Open provider settings to check this member.",
    OK: "",
}


@dataclass(frozen=True)
class MemberFailure:
    """A classified member-call failure. ``status`` is one of :data:`STATUSES`
    (never ``ok`` for a real failure); ``detail`` is redacted; ``remediation`` is
    the one-step fix keyed off the status."""

    status: str
    detail: str
    remediation: str

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail, "remediation": self.remediation}


def _redact(text: str) -> str:
    """Defense-in-depth redaction of any string that leaves the gateway/loop."""
    from errorta_diagnostics import redact

    safe, _ = redact.redact_home_path(text or "")
    safe, _ = redact.redact_username(safe)
    safe, _ = redact.redact_ssh_host(safe)
    safe, _ = redact.redact_ips(safe)
    safe, _ = redact.redact_tokens(safe)
    return safe[:200]


def _classify_message(msg: str) -> str:
    """Map a gateway exception/message string to a status. Order matters:
    binary-missing and auth patterns are checked before the generic buckets so a
    message that mentions both (rare) resolves to the more specific reason."""
    low = msg.lower()

    # binary_missing — the CLI is not installed / not found.
    if (
        "_not_installed" in low
        or "_not_found" in low
        or "binary not found" in low
        or "not on path" in low
        or "is not on path" in low
    ):
        return BINARY_MISSING

    # model_rejected — the provider refuses the requested model id (renamed,
    # removed, or never valid). Retrying the same model never heals, so this is
    # terminal. Checked before auth/generic buckets so a clear model error never
    # masquerades as a login problem. Matches both the gateway's normalized
    # `*_model_rejected:` prefix and the raw provider phrasings (e.g. Cursor's
    # "Cannot use this model: gpt-5. Available models: ...").
    if (
        "model_rejected" in low
        or "cannot use this model" in low
        or "invalid model" in low
        or "unknown model" in low
        or "model not found" in low
        or "model not supported" in low
        or "is not supported" in low
        or "available models:" in low
    ):
        return MODEL_REJECTED

    # auth_failed — logged-out CLI, expired key, 401/403, "Please run /login".
    if (
        "_not_authenticated" in low
        or "not authenticated" in low
        or "please run /login" in low
        or "run /login" in low
        or "/login" in low
        or "log in" in low
        or "logged in" in low
        or "login" in low
        or "unauthorized" in low
        or "authentication_error" in low
        or "authentication" in low
        or "401" in low
        or "403" in low
    ):
        return AUTH_FAILED

    # rate_limited — 429 / usage limit / rate limit.
    if (
        "_rate_limited" in low
        or "rate limit" in low
        or ("rate" in low and "limit" in low)
        or "usage limit" in low
        or "429" in low
    ):
        return RATE_LIMITED

    # timeout — the kill cascade / RetryableError timeout.
    if "_timeout" in low or "timed out" in low or "timeout" in low:
        return TIMEOUT

    # unparseable — model returned no usable / well-formed output.
    if (
        "_unparseable_output" in low
        or "_empty_result" in low
        or "_empty_model" in low
        or "unparseable" in low
        or "empty result" in low
    ):
        return UNPARSEABLE

    # Any other recognized failure — never `ok`.
    return ERRORED


def classify_member_failure(exc: Any) -> MemberFailure:
    """Turn a member-call exception (or any object with a string form) into a
    typed :class:`MemberFailure`.

    The real gateway exceptions are ``errorta_council.gateway_local.{FatalError,
    RetryableError}`` whose messages carry the provider's classification (e.g.
    ``claude_cli_error: API Error: 401 … Please run /login`` for the live 401
    incident, ``claude_cli_not_installed: …`` for a missing binary). An
    unrecognized error maps to ``errored`` — never ``ok`` (criterion #1).
    """
    raw = str(exc) if exc is not None else ""
    status = _classify_message(raw)
    return MemberFailure(
        status=status,
        detail=_redact(raw),
        remediation=_REMEDIATION.get(status, _REMEDIATION[ERRORED]),
    )


def classify_aware_cap(status: str, policy: "CodingAutonomyPolicy") -> int:
    """How many consecutive failures of ``status`` the loop tolerates before it
    raises a blocking Problem.

    Terminal reasons (``auth_failed`` / ``binary_missing``) cap at 1 — they will
    not heal by retrying, so stop fast. Transient reasons (``timeout`` /
    ``rate_limited``) and the catch-alls (``unparseable`` / ``errored``) cap at
    ``policy.member_failure_limit`` to ride a single network blip (criterion #2,
    spec risk note "classify-aware caps")."""
    limit = max(1, int(getattr(policy, "member_failure_limit", 3)))
    if status in _TERMINAL_STATUSES:
        return 1
    return limit


# --------------------------------------------------------------------------- #
# Pre-run preflight (F120-04)
# --------------------------------------------------------------------------- #
# Provider classes that can be cheaply login-state probed before a run. Only
# CLI/subscription providers are preflighted by default (they have a known
# logged-out failure mode + a cheap probe); HTTP-key providers are skipped.
_PREFLIGHT_PROVIDER_CLASSES = frozenset({"claude_cli", "codex_cli", "cursor_cli"})


def _provider_class_of(route_id: str, provider_kind: str = "") -> str:
    """The provider class from a gateway route id (the prefix before the first
    '.'), e.g. ``claude_cli.opus`` -> ``claude_cli``."""
    rid = (route_id or "").strip()
    if "." in rid:
        return rid.split(".", 1)[0]
    return rid or (provider_kind or "")


def _probe_route_status(provider_class: str) -> MemberFailure:
    """Cheap login-state probe of ONE provider class via its handler. Returns an
    ``ok`` MemberFailure when healthy, else the classified failure. Never raises —
    a probe error is itself classified (fail-loud-but-bounded)."""
    try:
        from errorta_model_gateway.loop_bridge import run_coro
        from errorta_model_gateway.providers import async_registry

        async_registry.ensure_bootstrapped()
        handler = async_registry.get_handler(provider_class)
        if handler is None or not hasattr(handler, "probe_auth"):
            return MemberFailure(OK, "", "")
        result = run_coro(handler.probe_auth())
        state = str(result.get("state", "")) if isinstance(result, dict) else ""
        if state == "connected":
            return MemberFailure(OK, "", "")
        if state == "logged_out":
            return MemberFailure(
                AUTH_FAILED, _redact(str((result or {}).get("detail", ""))),
                _REMEDIATION[AUTH_FAILED])
        # Any other non-connected state surfaces as a classified failure off its
        # detail string so the unhealthy reason is still actionable.
        detail = str((result or {}).get("detail", "")) if isinstance(result, dict) else ""
        failure = classify_member_failure(detail or "provider unhealthy")
        if failure.status == OK:  # defensive — never let a probe read as ok
            return MemberFailure(ERRORED, _redact(detail), _REMEDIATION[ERRORED])
        return failure
    except Exception as exc:  # noqa: BLE001 — classify probe errors, never crash start
        return classify_member_failure(exc)


def preflight_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Probe each DISTINCT preflightable provider route in the room once.

    Returns a list of unhealthy entries — one per provider class that failed —
    each ``{provider, route, reason, detail, remediation, member_ids}``. An empty
    list means every preflightable route is healthy (or there were none). Probing
    is deduped per provider class to bound cost (criterion #6)."""
    # route_id -> (provider_class, [member_ids]); we dedupe the PROBE per
    # provider class but report the member_ids that use it.
    by_class: dict[str, dict[str, Any]] = {}
    multi_unhealthy: list[dict[str, Any]] = []
    for m in members:
        if not m.get("enabled", True):
            continue
        if str(m.get("model_mode") or "single") == "multi":
            from .model_availability import (
                available_route_ids,
                resolve_route_availability,
            )

            pool = [str(route) for route in m.get("model_pool", []) if str(route)]
            projection = resolve_route_availability(pool)
            if available_route_ids(projection):
                continue
            reasons = sorted({item.reason for item in projection.values() if item.reason})
            multi_unhealthy.append({
                "provider": "multi",
                "route": pool[0] if pool else "",
                "reason": "model_pool_unavailable",
                "detail": ", ".join(reasons) or "no models configured",
                "remediation": (
                    "Enable and connect at least one model family in Settings, "
                    "or edit this member's model pool."
                ),
                "member_ids": [str(m.get("id", ""))],
            })
            continue
        route = str(m.get("gateway_route_id") or "")
        pclass = _provider_class_of(route, str(m.get("provider_kind") or ""))
        if pclass not in _PREFLIGHT_PROVIDER_CLASSES:
            continue
        entry = by_class.setdefault(
            pclass, {"routes": set(), "member_ids": []})
        entry["routes"].add(route)
        entry["member_ids"].append(str(m.get("id", "")))

    unhealthy: list[dict[str, Any]] = list(multi_unhealthy)
    for pclass, info in by_class.items():
        failure = _probe_route_status(pclass)
        if failure.status == OK:
            continue
        unhealthy.append({
            "provider": pclass,
            "route": sorted(r for r in info["routes"] if r)[:1][0]
            if any(info["routes"]) else pclass,
            "reason": failure.status,
            "detail": failure.detail,
            "remediation": failure.remediation,
            "member_ids": info["member_ids"],
        })
    return unhealthy


__all__ = [
    "MemberFailure",
    "classify_member_failure",
    "classify_aware_cap",
    "preflight_members",
    "STATUSES",
    "OK",
    "AUTH_FAILED",
    "BINARY_MISSING",
    "MODEL_REJECTED",
    "TIMEOUT",
    "RATE_LIMITED",
    "UNPARSEABLE",
    "ERRORED",
]
