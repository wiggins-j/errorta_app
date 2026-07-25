"""Spec 15 — a role-capability manifest, derived not authored.

The gravity-golf run's PM planned a task titled *"Run acceptance gate and fix
failures"* and assigned it to a DEV. A DEV has exactly one tool — ``code_write``
(``turn_controller._ROLE_TOOLS``). The task was unsatisfiable as written, and the
loop had no way to notice: it dispatched, the dev wrote code (all it can do), the
reviewer rejected for missing execution evidence, a ``revise:`` task was spawned,
and the cycle repeated for the last ~20 minutes of the run.

Nothing in the pipeline modelled *what each role can do*. This module is that
model. It is **pure and read-only**: it *describes* capabilities from the code
that actually enforces them (``allowed_tools_for_role`` plus the live policy
flags) and classifies one narrow, high-signal class of task text — *"produce
evidence by executing something"* — so the planner and the reviewer-rejection
seam can route or refuse it instead of handing it to a role that cannot discharge
it.

Derivation, not authorship, is the point: when ``_ROLE_TOOLS`` changes, every
prompt that describes it changes with it, and the F087-14 WS-3 discipline
("advertise ONLY tools that are actually executed") extends from the tool catalog
to the planner.

Import discipline (F159 ``paths.py``): imports ``.topology`` / ``.turn_controller``
/ ``.gate_state`` only — never ``runner`` (``runner`` imports this).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import gate_state
from .topology import DEV, PM, REVIEWER, TESTER
from .turn_controller import allowed_tools_for_role


@dataclass(frozen=True)
class RoleCapability:
    """What a single role can and cannot do, derived from live enforcement."""

    role: str
    tools: tuple[str, ...]
    repo_read: bool          # in-turn read-only retrieval active for this role
    can_execute: bool        # can this role run a command from inside a turn?
    gate_available: bool     # is there an acceptance gate that CAN produce evidence?
    summary: str             # one-line "can / cannot"

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role, "tools": list(self.tools),
            "repo_read": self.repo_read, "can_execute": self.can_execute,
            "gate_available": self.gate_available, "summary": self.summary,
        }


# No role can run a command from inside a turn: execution is engine-driven (the
# F087-14 WS-3 rationale — the gate/tester runs commands, roles consume the
# output). This is a structural fact, not a policy, so it is a constant here.
_CAN_EXECUTE: dict[str, bool] = {PM: False, DEV: False, REVIEWER: False, TESTER: False}


def _repo_read_for(role: str, policy) -> bool:
    # Read both cross-branch flags defensively: `reviewer_repo_read` is added by
    # the shared prep and consumed by Spec 14; absent -> False.
    if role == DEV:
        return bool(getattr(policy, "dev_repo_read", False))
    if role == REVIEWER:
        return bool(getattr(policy, "reviewer_repo_read", False))
    return False


def _summary_for(role: str, tools: tuple[str, ...], repo_read: bool,
                 gate_available: bool) -> str:
    tool_txt = ", ".join(tools) or "no write/exec tools"
    read_txt = " + read-only repo retrieval" if repo_read else ""
    if role == DEV:
        return (f"writes code ({tool_txt}{read_txt}); cannot run commands — "
                "execution evidence comes from the acceptance gate"
                + ("" if gate_available else " (none configured yet)"))
    if role == REVIEWER:
        return (f"judges the diff ({tool_txt}{read_txt}); cannot run commands — "
                "reads the gate output, never produces it")
    if role == TESTER:
        return ("runs the registered test commands via the engine gate; does not "
                "author code or tests")
    if role == PM:
        return ("plans and steers; creates tasks but cannot run commands or write "
                "code — must route execution work to the gate")
    return f"{tool_txt}{read_txt}"


def capability_manifest(store, policy=None) -> dict[str, RoleCapability]:
    """The single source of truth for role capabilities, derived from
    ``_ROLE_TOOLS`` + the live policy flags + whether a gate exists. Pure.

    ``policy`` may be ``None`` (``getattr(None, ...)`` yields the ``False``
    default) — the PM prompt path has no policy object, and the repo-read flags
    are a refinement, not load-bearing for the 'no role can execute' message."""
    try:
        gate = gate_state.gate_available(store)
    except Exception:  # noqa: BLE001 — a manifest must never fail a turn
        gate = False
    manifest: dict[str, RoleCapability] = {}
    for role in (PM, DEV, REVIEWER, TESTER):
        tools = tuple(allowed_tools_for_role(role))
        repo_read = _repo_read_for(role, policy)
        manifest[role] = RoleCapability(
            role=role, tools=tools, repo_read=repo_read,
            can_execute=_CAN_EXECUTE.get(role, False), gate_available=gate,
            summary=_summary_for(role, tools, repo_read, gate))
    return manifest


def pm_capability_segment(store, policy=None) -> str:
    """The PM ``tool_guidance`` text: each role's real surface, and the rule that
    no role can run a command from inside a turn — so execution evidence must come
    from the acceptance gate, never from a task that asks a DEV to 'run' anything."""
    man = capability_manifest(store, policy)
    lines = ["Role capabilities (what each role can actually do this run):"]
    for role in (PM, DEV, REVIEWER, TESTER):
        lines.append(f"- {role}: {man[role].summary}")
    gate = man[DEV].gate_available
    lines.append(
        "No role can run a command, launch the app, or execute tests from inside "
        "a turn. Execution evidence is produced ONLY by the acceptance gate"
        + (" (configured)." if gate else " (not yet configured this run)."))
    lines.append(
        "So do NOT create a task that asks a DEV/REVIEWER to run, launch, execute, "
        "measure, or 'verify by running' and report the result — write it as work "
        "the gate can verify (e.g. 'add a test that fails on trivial levels'), or "
        "rely on the gate output. A 'run X and report' task cannot be discharged.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The execution-imperative classifier — the narrow, high-signal lint. This table
# IS the spec of the lint (see the spec's Item 2 test table).
# --------------------------------------------------------------------------- #

# A verb about *running* the artifact.
_RUN_VERBS = (
    "run", "execute", "launch", "measure", "benchmark", "profile",
    "reproduce", "rerun", "re-run",
)
# A demand to *see the product of a run* — output, a table, pass/fail, a metric.
_EVIDENCE_TERMS = (
    "paste", "report", "output", "outputs", "results", "result", "table",
    "passes", "passing", "failures", "failure", "failing", "logs", "log",
    "screenshot", "metric", "metrics", "prove", "proof", "evidence",
)
# Authoring is normal, valuable DEV work and is explicitly whitelisted: WRITE a
# test, ADD a harness. Checked FIRST so "write a script that runs the levels" is
# authoring, not execution.
_AUTHORING_VERBS = (
    "write", "add", "create", "implement", "author", "scaffold", "define",
)
_AUTHORING_NOUNS = (
    "test", "tests", "harness", "gate", "suite", "benchmark", "fixture",
    "script", "spec", "assertion", "assertions", "check", "checks",
)


def _has_word(text: str, words) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def classify_task_text(title: str, detail: str = "") -> str:
    """Classify one task/finding text as ``"execution"`` | ``"authoring"`` |
    ``"other"``.

    * ``"authoring"`` — write/add/create a test/gate/harness/… . Normal DEV work.
      Checked first so an authoring verb wins over a ``benchmark``-as-run-verb.
    * ``"execution"`` — a run-verb (run/execute/launch/measure/…) AND an evidence
      demand (report/output/results/passes/…). BOTH halves required, so "write a
      script that runs the levels" (authoring) and "run the linter" (no evidence
      half) do not match.
    * ``"other"`` — everything else; not linted.
    """
    text = f"{title or ''} {detail or ''}".lower()
    if _has_word(text, _AUTHORING_VERBS) and _has_word(text, _AUTHORING_NOUNS):
        return "authoring"
    if _has_word(text, _RUN_VERBS) and _has_word(text, _EVIDENCE_TERMS):
        return "execution"
    return "other"


GATE_FIX_TITLE = "Fix the failures reported by the acceptance gate"


def routed_execution_task(original_title: str) -> tuple[str, str]:
    """The gate-available rewrite of an execution-imperative DEV task: keep the
    'fix' half, drop the unsatisfiable 'run' half, and point the dev at the gate
    output. Returns ``(new_title, new_detail)``."""
    detail = (
        f"(Rewritten from {original_title!r}: no role can run a command from inside "
        "a turn, so 'run X and report' is not a DEV task.) The acceptance gate runs "
        "automatically; its latest output is in your prompt. Fix the failures it "
        "reports. If the gate is green, there is nothing to fix — close this task.")
    return GATE_FIX_TITLE, detail


__all__ = [
    "RoleCapability", "capability_manifest", "pm_capability_segment",
    "classify_task_text", "routed_execution_task", "GATE_FIX_TITLE",
]
