"""F143 / F143-01 — pure token-usage rollup over coding-ledger turns.

The single place per-turn token counts are summed. ``regenerate_digest`` caches
this module's ``total`` on ``digest.json``; the ``/usage-summary`` route serves the
full projection. No I/O, no egress — deterministic given a turn list, so it is
fixture-testable like the rest of the ledger math.

F143-01 Slice D — the headline is now GENUINE + honest about coverage:

* ``input``/``output`` are the EFFECTIVE headline sums — measured-where-present,
  estimated otherwise. Every turn that has a usage block contributes its effective
  ``input_tokens``/``output_tokens`` (Slice C already made these effective), so a
  dark-provider DEV turn's estimated spend lands in the total instead of being
  dropped as a silent zero (the motivating bug).
* ``measured_input``/``measured_output`` are the provider-reported portion.
* ``estimated_input``/``estimated_output`` are the ESTIMATED PORTION ACTUALLY IN THE
  HEADLINE, computed per turn as ``effective - measured_portion`` — robust across
  measured (0 estimated), estimated (whole estimate), and partial (estimated side
  only) turns. It is deliberately NOT a raw sum of ``usage.estimated_input`` (that
  double-counts measured turns, which also carry an estimate for cli_overhead /
  calibration).
* ``coverage`` = ``{measured_pct, estimated_pct}`` — the SHARE OF HEADLINE TOKENS
  (not turn count) that is measured vs estimated.
* Provenance counts (``measured_turns``/``partial_turns``/``estimated_turns``/
  ``unreported_turns``) bucket each turn by its ``usage.provenance`` (a turn with no
  usage block counts as ``unreported_turns`` and contributes 0 tokens).

The headline everywhere is ``input`` + ``output`` (F143 invariant 4 / D4); cache
tokens are summed separately (``cache_read``/``cache_write``) and surfaced only as a
per-turn detail, never folded into the headline input number.
"""
from __future__ import annotations

from typing import Any


def _empty_bucket() -> dict[str, Any]:
    return {
        # Headline (effective) sums.
        "input": 0,
        "output": 0,
        # Split of the headline into its measured vs estimated portions.
        "measured_input": 0,
        "measured_output": 0,
        "estimated_input": 0,
        "estimated_output": 0,
        # Detail only — never folded into input/output (D4).
        "cache_read": 0,
        "cache_write": 0,
        # Turn + provenance counts.
        "turns": 0,
        "measured_turns": 0,
        "partial_turns": 0,
        "estimated_turns": 0,
        "unreported_turns": 0,
        # Share of headline tokens (not turn count) measured vs estimated.
        "coverage": {"measured_pct": 0, "estimated_pct": 0},
    }


def _coerce_int(value: Any) -> int | None:
    """A token field is either a non-negative int or absent. Anything else
    (None, str, negative, bool) is treated as absent — the rollup never
    fabricates a count."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _provenance_of(usage: dict[str, Any] | None) -> str:
    """The turn's honest provenance. A turn with no usage block — or a usage block
    that reports no usable numbers — is ``unreported``."""
    if not isinstance(usage, dict):
        return "unreported"
    prov = usage.get("provenance")
    if isinstance(prov, str) and prov in (
            "measured", "measured_partial", "estimated", "unreported"):
        return prov
    # Legacy F143 blocks (no provenance field): infer from measured + numbers so
    # older ledgers still count correctly.
    inp = _coerce_int(usage.get("input_tokens"))
    out = _coerce_int(usage.get("output_tokens"))
    if bool(usage.get("measured")) and (inp is not None or out is not None):
        return "measured" if (inp is not None and out is not None) \
            else "measured_partial"
    if inp is not None or out is not None:
        return "estimated"
    return "unreported"


def _accumulate(bucket: dict[str, Any], usage: dict[str, Any] | None) -> None:
    bucket["turns"] += 1
    prov = _provenance_of(usage)

    if prov == "measured":
        bucket["measured_turns"] += 1
    elif prov == "measured_partial":
        bucket["partial_turns"] += 1
    elif prov == "estimated":
        bucket["estimated_turns"] += 1
    else:  # unreported
        bucket["unreported_turns"] += 1

    if not isinstance(usage, dict) or prov == "unreported":
        # A legacy/unreported turn contributes 0 tokens but is still counted above.
        return

    # Effective headline ints (Slice C already made these measured-else-estimated).
    eff_in = _coerce_int(usage.get("input_tokens")) or 0
    eff_out = _coerce_int(usage.get("output_tokens")) or 0
    bucket["input"] += eff_in
    bucket["output"] += eff_out

    # Measured portion of the headline (present only on measured/partial turns).
    if ("measured_input" not in usage and "measured_output" not in usage
            and prov in ("measured", "measured_partial")):
        # Legacy F143 block: it predates byte-estimation, so its input_tokens/
        # output_tokens WERE the provider-reported counts (that is what measured=True
        # meant). Attribute the whole headline as measured rather than inverting real
        # provider spend into 100% "estimated".
        m_in, m_out = eff_in, eff_out
    else:
        m_in = _coerce_int(usage.get("measured_input")) or 0
        m_out = _coerce_int(usage.get("measured_output")) or 0
    # Never let a stray measured value exceed the effective (it shouldn't; guard math).
    m_in = min(m_in, eff_in)
    m_out = min(m_out, eff_out)
    bucket["measured_input"] += m_in
    bucket["measured_output"] += m_out

    # Estimated portion ACTUALLY IN THE HEADLINE = effective - measured portion.
    # (Not a raw sum of usage.estimated_input — that double-counts measured turns.)
    bucket["estimated_input"] += eff_in - m_in
    bucket["estimated_output"] += eff_out - m_out

    # Cache is detail only (D4) — never in input/output.
    bucket["cache_read"] += _coerce_int(usage.get("cache_read_input_tokens")) or 0
    bucket["cache_write"] += _coerce_int(usage.get("cache_write_input_tokens")) or 0


def _finalize_coverage(bucket: dict[str, Any]) -> None:
    """Compute the measured/estimated share of the bucket's HEADLINE tokens. Guarded
    against divide-by-zero (0 headline tokens → 0%/0%)."""
    total = bucket["input"] + bucket["output"]
    if total <= 0:
        bucket["coverage"] = {"measured_pct": 0, "estimated_pct": 0}
        return
    measured = bucket["measured_input"] + bucket["measured_output"]
    measured_pct = round(100 * measured / total)
    bucket["coverage"] = {
        "measured_pct": measured_pct,
        "estimated_pct": 100 - measured_pct,
    }


def _route_of(turn: dict[str, Any]) -> str:
    # F143-01 Slice A: PREFER the first-class ``route_id`` (the resolved route the
    # gateway actually dispatched to, stamped on every member turn incl. those that
    # skip the F129 assignment gate), then the F129 assignment's ``route_id``, then
    # the ``member_route`` hint. Token math is unchanged — only the bucket key.
    first_class = turn.get("route_id")
    if isinstance(first_class, str) and first_class.strip():
        return first_class.strip()
    ma = turn.get("model_assignment")
    if isinstance(ma, dict) and ma.get("route_id"):
        return str(ma["route_id"])
    # Fall back to any recorded route hint, else a stable "unknown" bucket so a
    # routeless turn's tokens are never dropped from the total.
    return str(turn.get("member_route") or "unknown")


def _role_of(turn: dict[str, Any]) -> str:
    role = turn.get("role")
    if isinstance(role, str) and role.strip():
        return role.strip()
    return "unknown"


def rollup_turns(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a coding project's ``turns.jsonl`` records into token buckets.

    Returns ``{"by_member": {member_id: bucket}, "by_route": {route_id: bucket},
    "by_role": {role: bucket}, "total": bucket}`` where each bucket carries the
    effective headline (``input``/``output``), its measured/estimated split, cache
    detail, turn + provenance counts, and a ``coverage`` share. ``input``/``output``
    are the *effective* (measured-else-estimated) sums; ``turns`` counts every turn;
    the four provenance counts partition the turns; ``coverage`` is the measured vs
    estimated share of the HEADLINE tokens.
    """
    by_member: dict[str, dict[str, Any]] = {}
    by_route: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, Any]] = {}
    total = _empty_bucket()

    for turn in turns:
        if not isinstance(turn, dict):
            continue
        usage = turn.get("usage")
        member_id = str(turn.get("member_id") or "unknown")
        route_id = _route_of(turn)
        role = _role_of(turn)

        _accumulate(total, usage)
        _accumulate(by_member.setdefault(member_id, _empty_bucket()), usage)
        _accumulate(by_route.setdefault(route_id, _empty_bucket()), usage)
        _accumulate(by_role.setdefault(role, _empty_bucket()), usage)

    for bucket in (total, *by_member.values(), *by_route.values(),
                   *by_role.values()):
        _finalize_coverage(bucket)

    return {
        "by_member": by_member,
        "by_route": by_route,
        "by_role": by_role,
        "total": total,
    }
