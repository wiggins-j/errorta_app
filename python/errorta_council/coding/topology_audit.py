"""GL05 (Item 1) — the role-justification topology audit (RQ5).

The multi-agent-failure report's RQ5 asks *when does decomposing the loop into
council roles pay?* Its answer, made into a design principle:

    Every council role must justify its seat as either
      (a) independently-verifiable, context-light coordination work, or
      (b) a DISTINCT SIGNAL (grounded verification, strategic review)
    — never "another ungrounded opinion in the same loop".

This module is that principle made into a lightweight, pure, testable audit. It
scores each SEATED role against the rule using the SPEC-15 ``capability_manifest``
(does the role add a distinct capability/signal, or is it a redundant opinion?)
and surfaces an advisory for a role that fails — reusing GL03's grant-or-delete
framing (``capabilities.audit_grant_or_delete``, *composed* not duplicated): grant
the missing signal or collapse the role.

This module itself is still **advisory** — it scores and phrases, it does not act.
What CHANGED (SPEC-26) is what happens to its verdict. It used to be the whole
story: the runner wrote a decision, raised a non-blocking Alert, and ran the
un-capable role anyway, so the same advisory fired on every run and never
resolved. SPEC-26 layers ``capabilities.role_closure`` on top of the very same
``audit_grant_or_delete`` text this module composes, classifies each violation as
``capable``/``deferred``/``unclosable``, and gives it a consequence at seat time:
the role is **UNSEATED** for the run (not refused — a refusal would refuse the
product's own shipped defaults on every new project), unless
``policy.capability_overrides`` names it. A ``deferred`` role is re-evaluated after
every merge and re-seated — and this module's Alert dismissed — when its capability
actually arrives. The PM+DEV baseline — single-agent-plus-coordination — always
passes; that is the very topology the RQ5 principle defends, and SPEC-26 must never
make a PM+DEV run ask the operator anything.

Pure and read-only: it consumes a manifest dict + a seated-role set and returns
verdicts / messages. The runner owns the ledger writes and the attention signal.

Import discipline (F159 ``paths.py``): imports ``.capabilities`` / ``.topology``
only — never ``runner`` (``runner`` imports this).
"""
from __future__ import annotations

from dataclasses import dataclass

from .capabilities import RoleCapability, audit_grant_or_delete
from .topology import DEV, PM, REVIEWER, TESTER

# The distinct signal each ADDED role must supply to earn its seat. PM and DEV are
# not "added" roles — PM is context-light coordination (category a) and DEV is the
# single-agent producer (the baseline) — so they have no distinct signal to justify
# and always pass. This is exactly the lens GL01 (execution) and GL02 (grounded
# review) already pass, and the one the failed run's write-only DEVs + blind
# REVIEWER failed.
_DISTINCT_SIGNAL = {
    TESTER: "execution",          # GL01 — a real executor/gate result
    REVIEWER: "grounded review",  # GL02 — repo/execution-linked verification
}

# Why PM/DEV never need a distinct-signal justification.
_BASELINE_CATEGORY = {
    PM: "coordination",   # (a) independently-verifiable, context-light sequencing
    DEV: "producer",      # the single-agent baseline itself, not an added role
}


@dataclass(frozen=True)
class RoleJustification:
    """One seated role's verdict against the role-justification principle."""

    role: str
    justified: bool
    category: str    # "coordination" | "producer" | "distinct-signal"
    signal: str      # the distinct signal it supplies / the one it lacks ("" for baseline)
    reason: str      # one legible line


def _baseline_reason(role: str) -> str:
    if role == PM:
        return ("PM coordinates independently-verifiable, context-light work "
                "(assignment, merge sequencing) — category (a), always justified")
    return ("DEV is the single-agent producer (the baseline), not an added role — "
            "always justified")


def audit_topology(
    manifest: dict[str, RoleCapability],
    *, seated_roles: tuple[str, ...] | None = None,
) -> list[RoleJustification]:
    """Score each SEATED role against the role-justification principle. Pure.

    ``seated_roles`` defaults to every role in the manifest. PM and DEV are the
    baseline and always pass. TESTER must supply the distinct signal *execution*;
    REVIEWER must supply *grounded review* — a role lacking its signal per the
    manifest is a redundant seat ("another ungrounded opinion in the same loop")
    and is flagged unjustified.

    Composes GL03's ``audit_grant_or_delete`` as the capability check: a
    single-role dispatch view yields the exact grant-or-delete message when the
    justifying signal is absent, so the enforcement text stays in one place.
    """
    seated = tuple(seated_roles) if seated_roles is not None else tuple(manifest)
    out: list[RoleJustification] = []
    for role in seated:
        cap = manifest.get(role)
        if cap is None:
            continue
        if role in _BASELINE_CATEGORY:
            out.append(RoleJustification(
                role=role, justified=True, category=_BASELINE_CATEGORY[role],
                signal="", reason=_baseline_reason(role)))
            continue
        signal = _DISTINCT_SIGNAL.get(role, "distinct signal")
        # GL03's grant-or-delete audit IS the capability check — scope it to this one
        # role so its verdict maps back cleanly. Empty == the role discharges its
        # duty (supplies the signal); a message == it does not (redundant seat).
        violations = audit_grant_or_delete(manifest, dispatched_roles=(role,))
        justified = not violations
        reason = (f"{role} supplies the distinct signal '{signal}' — justified"
                  if justified else violations[0])
        out.append(RoleJustification(
            role=role, justified=justified, category="distinct-signal",
            signal=signal, reason=reason))
    return out


def topology_advisories(
    manifest: dict[str, RoleCapability],
    *, seated_roles: tuple[str, ...] | None = None,
) -> list[str]:
    """The advisory surface: one legible line per SEATED role that fails the
    role-justification principle. Empty == every seat is earned.

    NOT a hard blocker — the caller records each line as a decision / non-blocking
    attention signal, never a refused run. The message is GL03's grant-or-delete
    framing (grant the missing distinct signal, or collapse the role)."""
    return [j.reason for j in audit_topology(manifest, seated_roles=seated_roles)
            if not j.justified]


__all__ = ["RoleJustification", "audit_topology", "topology_advisories"]
