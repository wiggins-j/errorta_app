"""F128 — completion gate: the read-only truth source for "is there open work?"

A PM ``done=true`` claim must be verified against the backlog before it is
accepted (``runner.py`` done-claim chokepoint). This module answers the only
question that gate needs: which tasks/PRs are still open?

READ-ONLY and pure — it never mutates the ledger. Fail-closed: any read error
returns a non-empty sentinel so the caller treats the project as NOT done (a
run that can't prove it's finished must not claim it is).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# D2 — terminal = finished/abandoned and does NOT block completion.
# Everything else is "open" (blocks done). `blocked` is open by design.
_TERMINAL_TASK_STATES = frozenset({"done", "dropped", "cancelled", "superseded"})
_TERMINAL_PR_STATES = frozenset(
    {"merged", "abandoned", "superseded", "closed", "dropped"}
)

# Open items a human (not the team) must resolve — surfaced distinctly so the
# UI/Problem can route them to a person instead of implying the team can retry.
_HUMAN_REQUIRED_TASK_STATES = frozenset({"blocked"})
_HUMAN_REQUIRED_PR_STATES = frozenset({"conflict", "blocked"})


@dataclass(frozen=True)
class OpenItem:
    """One backlog item that blocks completion."""
    kind: Literal["task", "pr", "unknown"]
    id: str
    title: str
    state: str
    human_required: bool


# Fail-closed sentinel: an unreadable backlog can't prove the project is done.
_UNREADABLE = (OpenItem(kind="unknown", id="", title="backlog unreadable",
                        state="", human_required=True),)


def pending_completion_work(ledger: Any) -> list[OpenItem]:
    """Return the items that block completion: non-terminal tasks + open PRs.

    READ-ONLY. Fail-closed — a read exception returns the ``_UNREADABLE``
    sentinel (non-empty) so the caller refuses a ``done`` claim rather than
    silently completing a project whose state it couldn't verify.
    """
    items: list[OpenItem] = []
    list_tasks = getattr(ledger, "list_tasks_strict", None)
    if not callable(list_tasks):
        list_tasks = getattr(ledger, "list_tasks", None)
    if not callable(list_tasks):
        return list(_UNREADABLE)
    try:
        for t in list_tasks():
            state = str(getattr(t, "state", "") or "")
            if state in _TERMINAL_TASK_STATES:
                continue
            items.append(OpenItem(
                kind="task",
                id=str(getattr(t, "task_id", "") or ""),
                title=str(getattr(t, "title", "") or getattr(t, "task_id", "") or "task"),
                state=state,
                human_required=state in _HUMAN_REQUIRED_TASK_STATES,
            ))
    except Exception:  # noqa: BLE001 — fail closed
        return list(_UNREADABLE)

    list_prs = getattr(ledger, "list_prs_strict", None)
    if not callable(list_prs):
        list_prs = getattr(ledger, "list_prs", None)
    if not callable(list_prs):
        return list(_UNREADABLE)
    try:
        for p in list_prs():
            status = str(p.get("status", "") or "")
            if status in _TERMINAL_PR_STATES:
                continue
            items.append(OpenItem(
                kind="pr",
                id=str(p.get("pr_id", "") or ""),
                title=str(p.get("branch") or p.get("task_id") or p.get("pr_id") or "PR"),
                state=status,
                human_required=status in _HUMAN_REQUIRED_PR_STATES,
            ))
    except Exception:  # noqa: BLE001 — fail closed
        return list(_UNREADABLE)

    return items


def summarize_open_items(items: list[OpenItem], cap: int = 8) -> str:
    """A compact, human-readable list of open items for a decision rationale /
    PM prompt / Problem summary. Caps the list and notes how many were dropped
    so a 250-deep backlog doesn't blow up the string (no silent truncation)."""
    if not items:
        return "no open items"
    shown = items[:cap]
    parts = []
    for it in shown:
        flag = " (human-required)" if it.human_required else ""
        parts.append(f"{it.kind} {it.title} [{it.state}]{flag}")
    extra = len(items) - len(shown)
    if extra > 0:
        parts.append(f"+{extra} more")
    return "; ".join(parts)


def count_human_required(items: list[OpenItem]) -> int:
    return sum(1 for it in items if it.human_required)


# SPEC-35 G2 — the acceptance gate's verdict for the `done` chokepoint.
AcceptanceGateStatus = Literal["no_gate", "green", "red", "stale"]


def acceptance_gate_status(ledger: Any, current_head: str) -> AcceptanceGateStatus:
    """SPEC-35 G2: classify the project's acceptance gate at ``current_head``.

    * ``no_gate``  — no acceptance-scoped command is registered (nothing to gate).
    * ``green``    — the gate's own latest result RAN and passed AT this head.
    * ``red``      — the gate's own latest result RAN and failed AT this head (a real
                     assertion failure the team can fix by editing code — fixable).
    * ``stale``    — a gate is registered but has no usable result at this head: it
                     ran at a different head, has never run, OR its latest result at
                     this head did not cleanly execute (a launch/provisioning failure
                     — timeout, blocked sandbox, missing interpreter). A launch
                     failure is deliberately NOT ``red``: it is environmental, no
                     code merge flips it green, so treating it as ``red`` would wedge
                     ``done`` forever. As ``stale`` it routes through the bounded
                     arm-and-refuse path (runner) that terminates in a human-routed
                     ``completion_blocked`` — never a silent permanent block.

    READ-ONLY and fail-open: any read error, or an unresolvable ``current_head``,
    returns ``no_gate`` so this can never INVENT a block (the module's fail-closed
    rule applies to open *work*; a `done`-block must fail *open* — a spurious block
    with no recovery is the wedge SPEC-34's review forbids). Recovery is structural:
    the in-loop gate re-runs the registered command every merge, so a ``red``/``stale``
    verdict flips to ``green`` on its own once the tree is fixed.
    """
    head = str(current_head or "")
    if not head:
        return "no_gate"  # cannot bind a result to a head -> never block
    try:
        cmds = ledger.get_test_commands() or {}
        has_acc = any(
            str((spec or {}).get("scope", "unit")) == "acceptance"
            for spec in cmds.values())
    except Exception:  # noqa: BLE001 — never invent a block
        return "no_gate"
    if not has_acc:
        return "no_gate"
    try:
        from .gate_state import latest_acceptance_result
        res = latest_acceptance_result(ledger)
    except Exception:  # noqa: BLE001
        return "no_gate"
    if not res:
        return "stale"  # registered but never run -> force a fresh run
    if str(res.get("head") or "") != head:
        return "stale"  # a result, but not for the tree we are about to call done
    if not res.get("ran", False):
        return "stale"  # a launch/provisioning failure, not a fixable assertion fail
    return "green" if res.get("passed") else "red"


# SPEC-40 (item E) — the declared-mechanic gate's verdict for the `done` chokepoint.
MechanicGateStatus = Literal["ok", "red", "advisory"]

_EVIDENCE_KEY = "probe_mechanic_evidence"


def _mechanic_evidence(ledger: Any, current_head: str) -> dict[str, Any] | None:
    """The newest MASTER-arm probe evidence, but only if it describes THIS tree.

    Evidence bound to a different head is evidence about a different artifact, so it
    is discarded rather than reused — the same head-binding discipline
    ``acceptance_gate_status`` applies. READ-ONLY; ``None`` on any read failure."""
    head = str(current_head or "")
    if not head:
        return None
    try:
        raw = ledger.get_run_state().get(_EVIDENCE_KEY)
    except Exception:  # noqa: BLE001 — never invent a block
        return None
    if not isinstance(raw, dict):
        return None
    if str(raw.get("head") or "") != head:
        return None
    return raw


def mechanic_gate_status(ledger: Any, current_head: str) -> MechanicGateStatus:
    """SPEC-40 item E: the four-path hierarchy for a declared-mechanic project.

    1. white-box contract present and GREEN -> ``ok``. This OVERRIDES a red advisory
       differential: a white-box, council-authored, game-native assertion is strictly
       stronger evidence than a black-box endpoint heuristic.
    2. contract present and RED -> ``red`` (recoverable; the caller routes it through
       the bounded ``completion_refused`` ladder).
    3. no contract + a CONFIDENT calibrated-inert differential -> ``red``. This is the
       golf-2 protection for a game that HAS a hook: a genuinely inert
       declared-mechanic game must not ship.
    3b. no usable ``__probe`` hook AT ALL -> ``red``. Distinct from path 3 and NOT
       subject to the confidence rule, because this is not a measurement that could be
       marginal — it is the total absence of behavioral evidence about a project that
       declared the claim. SPEC-37 blocked this, and the real gravity-golf-2 tree is
       exactly this shape (it predates the hook contract, so the differential never
       runs and there is no ``mechanic_matters`` reading to be confident about).
       Folding it into path 4 would let golf-2 ship on ``advisory`` — the precise
       failure regression lock 1 exists to prevent.
    4. everything else -> ``advisory``. Never a hard block, never an anchor
       regression. This is the golf-4 lesson — the oracle itself may be the thing
       that is wrong, so an uncertain verdict must not terminate a healthy run.

    READ-ONLY and FAIL-OPEN: missing evidence, a head mismatch, or any read error
    returns ``advisory``. The module's fail-closed rule governs open *work*; a
    ``done``-block must fail OPEN, because a spurious block with no recovery is
    precisely the wedge SPEC-34's review forbids.
    """
    ev = _mechanic_evidence(ledger, current_head)
    if ev is None:
        return "advisory"
    wb = str(ev.get("whitebox") or "absent")
    if wb == "green":
        return "ok"
    if wb == "red":
        return "red"
    # Path 3 — a MEASURED inert verdict. Marginal readings stay advisory.
    if ev.get("mechanic_matters") is False and bool(ev.get("confident")):
        return "red"
    # Path 3b — no measurement was possible at all. `mechanic_ok` is SPEC-37's verdict,
    # which is False for a missing or structurally unusable hook (and True for every
    # cannot-verify: a timeout, a thrown evaluation, an exhausted budget). Guarded on
    # `mechanic_matters is None` so a game that DID produce a reading is judged by
    # path 3's confidence rule and never sneaks a block in through here.
    if (ev.get("mechanic_matters") is None
            and ev.get("mechanic_ok") is False
            and not ev.get("has_hook")):
        return "red"
    return "advisory"


def mechanic_gate_reason(ledger: Any, current_head: str) -> str:
    """The actionable half of a ``red`` from :func:`mechanic_gate_status`.

    Returns the specific white-box arm's reason when there is one (naming
    ``setMechanic`` for the vacuity case — the message golf-4 needed and never got),
    or the path-3 steer otherwise. Empty string when there is nothing to say."""
    ev = _mechanic_evidence(ledger, current_head)
    if ev is None:
        return ""
    reason = str(ev.get("whitebox_reason") or "").strip()
    if str(ev.get("whitebox") or "") == "red" and reason:
        return reason
    if ev.get("mechanic_matters") is False and bool(ev.get("confident")):
        return ("the calibrated differential found no effect at any power in the "
                "game's own usable range — a straight shot behaves identically with "
                "the mechanic on vs off")
    # Path 3b — SPEC-37's reason already names the hook contract to build.
    if ev.get("mechanic_matters") is None and not ev.get("has_hook"):
        return str(ev.get("mechanic_reason") or "").strip()
    return ""


__all__ = [
    "OpenItem",
    "pending_completion_work",
    "summarize_open_items",
    "count_human_required",
    "acceptance_gate_status",
    "AcceptanceGateStatus",
    "mechanic_gate_status",
    "mechanic_gate_reason",
    "MechanicGateStatus",
]
