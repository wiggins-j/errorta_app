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
from .schemas import TurnErrorCode
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

# A verb about *running* the artifact. Deliberately excludes 'profile' — it is a
# noun far more often than a verb here ("runtime profile", "user profile", "profile
# page") and misclassified ordinary feature work as execution.
_RUN_VERBS = (
    "run", "execute", "launch", "measure", "benchmark",
    "reproduce", "rerun", "re-run",
)
# A demand to *see the product of a run* — output, pass/fail. Deliberately excludes
# 'table' / 'metric(s)': those are feature nouns ("results table", "user metrics")
# that a real UI task legitimately names, not a demand for run output.
_EVIDENCE_TERMS = (
    "paste", "report", "output", "outputs", "results", "result",
    "passes", "passing", "failures", "failure", "failing", "logs", "log",
    "screenshot", "prove", "proof", "evidence",
)
# Authoring is normal, valuable DEV work and is explicitly whitelisted: WRITE a
# test, ADD a harness. Checked FIRST so "write a script that runs the levels" is
# authoring, not execution.
_AUTHORING_VERBS = (
    "write", "add", "create", "implement", "build", "author", "scaffold", "define",
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

    Any authoring verb (add/create/build/write/…) short-circuits execution: BUILDING
    something is feature work, never a "run X and report" demand — so "Add a launch
    screen and a results table" is not execution even though it contains 'launch' and
    'results'. This is the guard against false positives on ordinary app vocabulary.
    """
    text = f"{title or ''} {detail or ''}".lower()
    if _has_word(text, _AUTHORING_VERBS):
        # Authoring a test/harness/gate is the whitelisted "authoring" class;
        # authoring anything else ("add a launch screen") is just "other" — either
        # way it is NOT an execution demand.
        return "authoring" if _has_word(text, _AUTHORING_NOUNS) else "other"
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


# --------------------------------------------------------------------------- #
# GL03 Item 1 — the confabulation detector (pure). An agent that invents a tool it
# was not granted (the gravity-golf DEV emitting a "run tests" call) is not just a
# logged failure — it is a capability-gap signal: the model told "run it" with no
# run tool does the coherent thing and confabulates the interface the task needs
# [3]. This classifier recognizes that signature and distinguishes it from a plain
# typo of a granted tool (which SPEC-17's corrective hint already handles). Pure:
# no ledger, no I/O — the runner owns the threshold/dedupe/alarm around it.
# --------------------------------------------------------------------------- #

# An invented tool name whose evident intent is to RUN the artifact.
_EXEC_TOOL_TOKENS = ("run", "test", "exec", "shell", "launch", "bench")
# ... or to READ the repo.
_READ_TOOL_TOKENS = ("read", "grep", "cat", "open", "ls")

# The gap-escalation threshold (spec §7 edge case, the single most important
# false-positive guard). A one-off confabulation is a fat-finger typo, not a
# systematic capability gap — only a REPEAT of the same ungranted tool on the same
# (role, capability) crosses into "the task needs an interface it lacks". The runner
# applies this before it pages the PM, so a single stray call never triggers a
# re-plan. Default: the SECOND identical gap event escalates.
GAP_ESCALATION_THRESHOLD = 2


@dataclass(frozen=True)
class ConfabulationSignal:
    """The detector's verdict on one failed DEV tool call.

    ``is_gap`` is true only for an ungranted-tool-call attempt whose intent maps to
    a capability class the role's manifest entry LACKS — never for a typo of a tool
    the role does have, nor for a write failure."""

    is_gap: bool
    capability: str | None   # "execution" | "read" | None
    why: str


def _name_tokens(name: str) -> tuple[str, ...]:
    """Split a tool name into lowercase word tokens, breaking on non-alphanumerics
    AND camelCase boundaries (``runTests`` → ``run``, ``tests``)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name or "")
    return tuple(t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t)


def _intent_matches(tokens: tuple[str, ...], class_tokens: tuple[str, ...]) -> bool:
    """A token maps to a capability class if it EQUALS a class token or has it as a
    PREFIX — so ``execute`` matches ``exec``, ``tests`` matches ``test``, and
    ``launcher`` matches ``launch``, while an interior substring does NOT (``cat``
    must not match ``frobnicate``/``concatenate``; ``ls`` must not match ``tools``).
    Matching whole split tokens, not the raw name, is what keeps ordinary words from
    tripping the alarm."""
    for tok in tokens:
        for cls in class_tokens:
            if tok == cls or tok.startswith(cls):
                return True
    return False


def confabulation_from_failure(
    role: str, tool_name: str, reason: str,
    manifest: dict[str, "RoleCapability"] | None,
) -> ConfabulationSignal:
    """Classify one failed DEV tool call. Pure.

    A gap requires ALL of:
    * ``reason`` is a ``tool_not_allowed`` rejection (the confabulation signature) —
      a ``write_failed`` (disk/parse) is deliberately ignored (spec §7);
    * the invented ``tool_name``'s evident intent maps to a capability CLASS
      (execution / read) that the role's manifest entry does NOT grant.

    So ``run_tests`` on a gate-less role → gap(execution); ``read_files`` with
    repo-read OFF → gap(read); the same with repo-read ON → NOT a gap (the role was
    granted Read, so it is a naming typo for SPEC-17); ``code_writ`` → NOT a gap
    (a typo of the granted ``code_write``); any ``write_failed`` → NOT a gap."""
    not_a_gap = ConfabulationSignal(is_gap=False, capability=None, why="")
    if not str(reason or "").startswith(TurnErrorCode.tool_not_allowed.value):
        return ConfabulationSignal(
            False, None, "not a tool_not_allowed rejection (write/parse failure)")
    cap = manifest.get(role) if manifest else None
    tokens = _name_tokens(tool_name)
    # Execution intent: run/test/exec/shell/launch/bench AND the role has no
    # gate/executor availability to discharge it.
    if _intent_matches(tokens, _EXEC_TOOL_TOKENS):
        has_exec = bool(cap and (cap.can_execute or cap.gate_available))
        if not has_exec:
            return ConfabulationSignal(
                True, "execution",
                f"{role} invented execution tool {tool_name!r}; its manifest grants "
                "no gate/executor capability — the task needs an execution interface")
        return not_a_gap  # a gate exists; SPEC-15 routes execution to it
    # Read intent: read/grep/cat/open/ls AND repo-read is OFF for this role.
    if _intent_matches(tokens, _READ_TOOL_TOKENS):
        if not (cap and cap.repo_read):
            return ConfabulationSignal(
                True, "read",
                f"{role} invented read tool {tool_name!r}; repo-read is off for this "
                "role — the task needs a read interface")
        return not_a_gap  # repo-read granted (SPEC-11/14); a naming typo for SPEC-17
    # Neither intent — a typo of a granted tool (code_writ) or an unknown name with
    # no capability signal. SPEC-17's corrective hint handles it, not GL03.
    return ConfabulationSignal(False, None, "no capability-gap intent in tool name")


# --------------------------------------------------------------------------- #
# GL03 Item 2 — grant-or-delete as an audited invariant. A role whose DUTY its
# manifest cannot discharge is not doing its job with degraded quality — it is
# doing a DIFFERENT job (a TESTER that never dispatches does nothing; a REVIEWER
# that cannot verify injects noise) [10][13]. The rule: GRANT the capability
# (SPEC-12 gave the TESTER the in-loop gate; SPEC-14 gave the REVIEWER repo-read) or
# DELETE the role from dispatch — never leave it dispatching into a wall. This audit
# makes the choice un-silently-regressible. It does NOT delete anything (a topology
# decision, out of scope); it FAILS LEGIBLY on an un-capable dispatched role.
# --------------------------------------------------------------------------- #

def audit_grant_or_delete(
    manifest: dict[str, "RoleCapability"],
    *, dispatched_roles: tuple[str, ...] | None = None,
) -> list[str]:
    """Return one legible message per grant-or-delete violation; empty == passes.

    For every DISPATCHED role, its manifest entry must grant a capability
    sufficient for its stated duty:
    * TESTER — duty demands execution -> needs gate/executor availability;
    * REVIEWER — duty demands verification -> needs read (or execute) capability;
    * DEV — duty demands authoring -> needs a write tool.
    A role absent from ``dispatched_roles`` is the DELETE half — not audited. PM
    plans via its intent (no tool), so it is always satisfied."""
    dispatched = (set(dispatched_roles) if dispatched_roles is not None
                  else set(manifest))
    violations: list[str] = []
    for role, cap in manifest.items():
        if role not in dispatched:
            continue
        if role == TESTER and not (cap.can_execute or cap.gate_available):
            violations.append(
                "role TESTER's duty demands execution but its manifest grants no "
                "gate/executor capability — grant it (SPEC-12) or delete the role")
        elif role == REVIEWER and not (cap.repo_read or cap.can_execute):
            violations.append(
                "role REVIEWER's duty demands verification but its manifest grants "
                "no read/execute capability — grant it (SPEC-14) or delete the role")
        elif role == DEV and not cap.tools:
            violations.append(
                "role DEV's duty demands authoring but its manifest grants no write "
                "tool — grant it or delete the role")
    return violations


__all__ = [
    "RoleCapability", "capability_manifest", "pm_capability_segment",
    "classify_task_text", "routed_execution_task", "GATE_FIX_TITLE",
    "ConfabulationSignal", "confabulation_from_failure", "GAP_ESCALATION_THRESHOLD",
    "audit_grant_or_delete",
]
