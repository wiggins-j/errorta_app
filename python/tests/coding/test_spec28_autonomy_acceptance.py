"""Spec 28 — end-to-end autonomy acceptance (Tier 1 + Tier 1b).

The roadmap's flat admission is that **no run has ever completed**: three live
gravity-golf runs, zero finished products, and thousands of unit tests in
``python/tests/coding/`` not one of which asserts that a run *finishes*. This file
is the aggregate assertion the batch exists to support — one buildless-web fixture
driven through the REAL loop (``CodingRunner`` -> ``build_run_turn`` ->
``run_coding_loop`` -> real workspace / branch / PR / merge gate / web probe) to
``definition_of_done``.

Determinism (Item 1): the model is the only non-deterministic component and the
engine already seams it (``CodingRunner(caller=...)``). :class:`ScriptedTeam`
answers from a state machine keyed on **(role, ledger state)** — never on a call
index, because turn ORDER is ``decide_next`` / ``plan_next_batch``'s to choose and
a positional script would silently test a different sequence than it claims. Every
emitted envelope is a real ``coding_turn.v1`` document that must survive
``parse_coding_turn`` unaided (B7: ``turns_repaired == 0``).

Friction (Item 3) is a REQUIREMENT, not a nicety: F1 a rejected review + a revise,
F2 a duplicate task, F3 the Spec 21 prune turn, F4 a context request, F5 >= 12
iterations. Each has its own meta-assertion, so an edit that quietly smooths the
transcript turns this file RED instead of hollowing the gate out.

Browser realism is tiered on purpose. Tier 1 seams ``_default_node_runner`` (the
same seam ``test_gl01_web_probe.py`` uses) and therefore proves *the probe arm is
reached by a real run, is bound to the delivered head, and its verdict decides the
outcome* — it does NOT prove pixels. Tier 1b removes the seam and drives real
Chromium; it skips when the toolchain is absent, exactly as GL01's probe smoke does.

WHY THE CANONICAL RUN PINS ``max_parallel_workers=1``. Writing this fixture found a
live composition defect, and the pin is the honest way to land around it rather than
through it: under fan-out, GL05's strict a-priori file-ownership partition
(``autonomy.inflight_owned_paths`` -> ``topology.plan_next_batch``) can never
dispatch a ``revise:`` task whose reviewer finding CITES a path, because the PR the
revise supersedes stays ``changes_requested`` — a live PR — and therefore keeps
owning that path until the revise merges. The run then plans forever and stops
``no_progress``. :func:`test_concurrent_fanout_wedges_a_path_citing_revise` pins
that behaviour precisely, with the fix instructions in its docstring, and
:func:`test_tier1_concurrent_fanout_completes` proves the same transcript completes
on the concurrent chain the moment the partition is not in the way — so BOTH loop
chains are covered (batch-plan regression lock 5).
"""
from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from errorta_council.coding import autonomy, web_probe
from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
    LoopCounters,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import CodingRunner

pytestmark = [pytest.mark.acceptance, pytest.mark.e2e, pytest.mark.blocking]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = Path(__file__).parent / "fixtures" / "spec28_gravity_golf"

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev-1", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-dev-2", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

NORTH_STAR = (
    "A buildless browser game: index.html plus relative ES-module scripts that "
    "paint a gravity-golf course onto a <canvas>."
)
DOD = "index.html opens in a browser and paints a non-black canvas with the HUD."

FOUNDATION_TASK = "scaffold the buildless web foundation"
# title -> the ONE file that slice rewrites. Every file already exists on master
# after the foundation lands, so the module graph is self-consistent at EVERY merge
# — which is what lets the REAL Tier 1b probe stay green mid-run, not only at the end.
FEATURE_TASKS: dict[str, str] = {
    "expand the level set to three holes": "src/levels.js",
    "paint the gravity well and the round HUD": "src/render.js",
    "sweep the aim indicator through a full turn": "src/input.js",
    "tune the launch physics constants": "src/main.js",
}
REJECTED_TASK = "expand the level set to three holes"      # F1: draws a blocking finding
CONTEXT_TASK = "paint the gravity well and the round HUD"  # F4: asks one question
PRUNE_DECISION = "drop the duplicate level-set task"       # F3 / L3
STEER = ("Keep the level data and the renderer in separate modules, please — do "
         "not merge them into one file.")


def _read(rel: str) -> str:
    return (_FIXTURE / rel).read_text(encoding="utf-8")


def _foundation_files() -> list[tuple[str, str]]:
    return [
        ("index.html", _read("index.html")),
        ("src/main.js", _read("drafts/main.foundation.js")),
        ("src/render.js", _read("drafts/render.foundation.js")),
        ("src/levels.js", _read("drafts/levels.foundation.js")),
        ("src/input.js", _read("drafts/input.foundation.js")),
    ]


# --------------------------------------------------------------------------- #
# Prompt readers + envelope builders (the `test_coding_runner.py` idiom)
# --------------------------------------------------------------------------- #
def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _delivery_head(prompt: str) -> str:
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _pm_env(*, tasks=None, decisions=None, done=False, completion_summary="") -> str:
    intent: dict[str, Any] = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if decisions is not None:
        intent["decisions"] = decisions
    if completion_summary:
        intent["completion_summary"] = completion_summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm",
                       "intent": intent})


def _dev_env(task_id: str, files) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": p, "content": c}}
                                  for p, c in files]}})


def _ctx_env(task_id: str, question: str, paths: list[str]) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "context_request", "reason": "missing_api_contract",
                   "question": question, "needed_for": "implementation",
                   "scope": {"paths": paths, "sources": ["memory", "corpus"]}}})


def _rev_env(task_id: str, head: str, *, approved=True, findings=None) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": findings or []}})


def _tester_env(task_id: str, command_ids) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": list(command_ids),
                   "scope": "full_project", "rationale": "run the acceptance gate"}})


# --------------------------------------------------------------------------- #
# The scripted team — keyed on (role, ledger state)
# --------------------------------------------------------------------------- #
class ScriptedTeam:
    """A healthy team, scripted. Every answer is derived from the LEDGER (which
    tasks exist, which PRs are live, which decisions were recorded), so reordering
    the schedule — or swapping the sequential loop for the concurrent one — cannot
    change what the team says."""

    def __init__(self, store: LedgerStore, *, friction: bool = True,
                 claim_done: bool = True) -> None:
        self.store = store
        self.friction = friction
        self.claim_done = claim_done
        self.unexpected: list[str] = []
        self.last_word_prompts: list[str] = []
        self.context_asks = 0
        self._steered = False

    # -- ledger readers ---------------------------------------------------- #
    def _tasks(self) -> list[Any]:
        return list(self.store.list_tasks())

    def _task(self, task_id: str) -> Any:
        for task in self._tasks():
            if task.task_id == task_id:
                return task
        raise AssertionError(f"unknown task id {task_id!r}")

    def _choices(self) -> list[str]:
        return [d.get("choice") for d in self.store.list_decisions()]

    def _decision_titles(self) -> list[str]:
        return [str(d.get("title") or "") for d in self.store.list_decisions()]

    def _merged_prs(self) -> int:
        return sum(1 for p in self.store.list_prs() if p.get("status") == "merged")

    def _open_dev_titles(self) -> list[str]:
        """Open (todo/doing) DEV tasks — the dedupe index's own definition of open."""
        return [t.title for t in self._tasks()
                if t.role == "dev" and t.state in ("todo", "doing")]

    def _pending(self) -> list[str]:
        out = [f"task {t.title}" for t in self._tasks()
               if t.state not in ("done", "dropped")]
        out += [f"pr {p.get('branch')}" for p in self.store.list_prs()
                if p.get("status") not in ("merged", "abandoned", "superseded",
                                           "blocked")]
        return sorted(out)

    def _origin_title(self, task: Any) -> str:
        """The DEV task a ``revise:`` task descends from (via its PR back-link)."""
        pr = self.store.get_pr(task.pr_id) if task.pr_id else None
        if pr is None:
            return ""
        for candidate in self._tasks():
            if candidate.task_id == str(pr.get("task_id") or ""):
                return candidate.title
        return ""

    # -- the caller -------------------------------------------------------- #
    def __call__(self, member: dict, prompt: str) -> str:
        # ORDER MATTERS: the last-word and PM-assist banners both contain
        # "You are the PM", so they are matched first.
        if "this run is about to STOP" in prompt:
            self.last_word_prompts.append(prompt)
            return self._last_word()
        if "You are the PM backstop" in prompt:
            self.unexpected.append("pm_assist")
            return _pm_env(decisions=[{"title": "unexpected PM assist",
                                       "rationale": "the fixture never plans one"}])
        if "You are the PM" in prompt:
            return self._pm()
        if "DELIVERY reviewer" in prompt:
            return _rev_env("delivery-review", _delivery_head(prompt), approved=True)
        if "You are a reviewer" in prompt:
            return self._review(prompt)
        if "You are a developer" in prompt:
            return self._dev(prompt)
        if "You are a tester" in prompt:
            return _tester_env(_task_id(prompt, "tester"), ["acceptance"])
        self.unexpected.append(prompt[:200])
        return "{}"

    # -- PM ---------------------------------------------------------------- #
    def _pm(self) -> str:
        titles = {t.title for t in self._tasks()}
        if FOUNDATION_TASK not in titles:
            return _pm_env(tasks=[{
                "title": FOUNDATION_TASK, "role": "dev",
                "detail": ("Create index.html plus src/main.js, src/render.js, "
                           "src/levels.js and src/input.js as relative ES modules. "
                           "Acceptance: index.html's <script src> graph resolves "
                           "entirely against files on master."),
            }])
        if self._merged_prs() < 1:
            return self._waiting()
        if not (set(FEATURE_TASKS) & titles):
            return _pm_env(tasks=[
                {"title": title, "role": "dev",
                 "detail": (f"Rewrite {path}. Acceptance: the module keeps its "
                            "existing exports so the rest of the graph still loads."),
                 "depends_on": []}
                for title, path in FEATURE_TASKS.items()
            ])
        # F2 — the duplicate batch. Scripted ONLY while a matching task is genuinely
        # OPEN (a ledger fact), never on a turn index: `_materialize_pm_tasks` has to
        # actually reject it, which is what drives `made_progress=False` -> pm_idle.
        if self.friction and "duplicate_task_rejected" not in self._choices():
            open_feature = next((t for t in self._open_dev_titles()
                                 if t in FEATURE_TASKS), None)
            if open_feature is not None:
                path = FEATURE_TASKS[open_feature]
                return _pm_env(tasks=[{
                    "title": open_feature, "role": "dev",
                    "detail": (f"Rewrite {path}. Acceptance: the module keeps its "
                               "existing exports so the rest of the graph still "
                               "loads."),
                }])
        # F3 / L3 — the Spec 21 prune turn, verbatim: `decision` (no `title`), no
        # tasks, not done. It must PARSE, must satisfy `_done_rules`, and must be
        # credited as progress rather than charged to `pm_idle`.
        if (self.friction and "duplicate_task_rejected" in self._choices()
                and PRUNE_DECISION not in self._decision_titles()):
            return json.dumps({
                "schema_version": "coding_turn.v1", "role": "pm",
                "intent": {"kind": "plan", "done": False, "tasks": [],
                           "decisions": [{
                               "decision": PRUNE_DECISION,
                               "rationale": ("the level-set slice is already open, "
                                             "so re-proposing it duplicates work; "
                                             "nothing new is queued this turn"),
                           }]}})
        if self._pending() or not self.claim_done:
            return self._waiting()
        return _pm_env(done=True, completion_summary=(
            "index.html and the four src/ modules are merged on master; the canvas "
            "paints the course, the gravity well and the HUD."))

    def _waiting(self) -> str:
        """A decision-only "what I am waiting on" turn. Its title is derived from the
        OPEN BACKLOG, so it stays novel while the backlog moves — which is what Spec
        25's novelty gate requires of a decision-only turn."""
        pending = self._pending()
        what = ", ".join(pending)[:160] if pending else (
            f"a sign-off after {len(self._decision_titles())} decisions")
        return _pm_env(decisions=[{
            "title": f"waiting on {what}",
            "rationale": "no new work is needed until that resolves",
        }])

    def _last_word(self) -> str:
        """A last word is only ever reached when a heuristic detector tripped. The
        healthy fixture must never need one, so this answers HONESTLY (confirming the
        halt) instead of papering the stop over with invented work."""
        return _pm_env(decisions=[{
            "title": "confirming the halt",
            "rationale": ("the scripted team has no further action; a last word on "
                          "this fixture means a detector tripped on a healthy run"),
        }])

    # -- DEV --------------------------------------------------------------- #
    def _dev(self, prompt: str) -> str:
        tid = _task_id(prompt, "developer")
        task = self._task(tid)
        title = task.title
        if title == FOUNDATION_TASK:
            return _dev_env(tid, _foundation_files())
        if title.startswith("revise:"):
            origin = self._origin_title(task)
            path = FEATURE_TASKS.get(origin)
            if path is None:
                self.unexpected.append(f"revise for unknown origin {origin!r}")
                path = FEATURE_TASKS[REJECTED_TASK]
            return _dev_env(tid, [(path, _read(path))])
        if title in FEATURE_TASKS:
            path = FEATURE_TASKS[title]
            # The user steers ONCE, mid-run, while the feature backlog is open. A
            # pending interjection preempts worker dispatch (`decide_next` step 0b),
            # which is what hands the PM a plan turn while a dev task is still open —
            # the exact state F2's duplicate needs, and a real user behaviour rather
            # than a scheduling trick.
            if self.friction and not self._steered:
                self._steered = True
                self.store.record_interjection(STEER)
            # F4 — one context request, on one slice, answered and then acted on.
            if (self.friction and title == CONTEXT_TASK
                    and _context_attempts(task) == 0):
                self.context_asks += 1
                return _ctx_env(
                    tid,
                    "Which canvas dimensions does main.js pass to draw(), and is "
                    "the HUD expected to paint inside the same canvas?",
                    ["src/main.js", "src/render.js"])
            # F1 — the first attempt at this slice carries the defect the reviewer
            # blocks on; the revise branch above writes the corrected module.
            if (self.friction and title == REJECTED_TASK
                    and not _has_revise_for(self.store, title)):
                return _dev_env(tid, [(path, _read("drafts/levels.rejected.js"))])
            return _dev_env(tid, [(path, _read(path))])
        self.unexpected.append(f"unscripted dev task {title!r}")
        return _dev_env(tid, [(f"notes/{tid}.md", "unscripted task\n")])

    # -- REVIEWER ---------------------------------------------------------- #
    def _review(self, prompt: str) -> str:
        tid = _task_id(prompt, "reviewer")
        task = self._task(tid)
        head = _pr_head(prompt)
        reviewed = task.title.split("review PR: ", 1)[-1].split(" (re-review", 1)[0]
        if (self.friction and reviewed == REJECTED_TASK
                and "review_rejected" not in self._choices()):
            # A CITED blocking finding: `path` is what makes it actionable, and an
            # actionable rejection is what spawns a `revise:` task rather than a
            # gate re-review.
            return _rev_env(tid, head, approved=False, findings=[{
                "severity": "blocking", "path": "src/levels.js",
                "title": "the third hole declares no par",
                "body": ("LEVELS[2] has no `par`, so parTotal() returns NaN and the "
                         "HUD renders 'par undefined'. Add par: 5."),
            }])
        return _rev_env(tid, head, approved=True)


def _context_attempts(task: Any) -> int:
    raw = (getattr(task, "_extras", {}) or {}).get("context_request_attempts")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _has_revise_for(store: LedgerStore, origin_title: str) -> bool:
    """True once a ``revise:`` task exists for ``origin_title`` — i.e. the first
    attempt has already been reviewed and rejected."""
    by_id = {t.task_id: t for t in store.list_tasks()}
    for task in by_id.values():
        if not task.title.startswith("revise:") or not task.pr_id:
            continue
        pr = store.get_pr(task.pr_id)
        origin = by_id.get(str((pr or {}).get("task_id") or ""))
        if origin is not None and origin.title == origin_title:
            return True
    return False


# --------------------------------------------------------------------------- #
# Counter tracing — the only way to observe a MAXIMUM rather than a final value
# --------------------------------------------------------------------------- #
class TracingCounters(LoopCounters):
    """``run_coding_loop`` accepts a caller-supplied counters object, so the fixture
    can watch the detector windows as they move. ``res.counters.pm_idle`` is the
    value at the END of the run (0 on a healthy one), while F2 and F3 are claims
    about the PEAK and about how many PM turns were charged as idle — which only a
    trace can answer."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Set BEFORE `super().__init__`, which assigns `pm_idle` and therefore
        # already runs the `__setattr__` hook below. (A dataclass's generated
        # `__init__` only calls `__post_init__` when the DECORATED class defines
        # one, so a subclass hook would never fire.)
        object.__setattr__(self, "max_pm_idle", 0)
        object.__setattr__(self, "pm_idle_charges", 0)
        object.__setattr__(self, "first_gate_iter", -1)
        super().__init__(*args, **kwargs)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "pm_idle":
            if value > getattr(self, "pm_idle", 0):
                object.__setattr__(self, "pm_idle_charges",
                                   getattr(self, "pm_idle_charges", 0) + 1)
            if value > getattr(self, "max_pm_idle", 0):
                object.__setattr__(self, "max_pm_idle", value)
        elif (name == "last_gate_best" and value >= 0
                and getattr(self, "first_gate_iter", -1) < 0):
            object.__setattr__(self, "first_gate_iter", getattr(self, "iterations", 0))
        object.__setattr__(self, name, value)


# --------------------------------------------------------------------------- #
# Running the fixture
# --------------------------------------------------------------------------- #
_ACCEPTANCE_ARGV = [
    sys.executable, "-c",
    "import pathlib,sys; t=pathlib.Path('src/levels.js').read_text(); "
    "sys.exit(0 if 'export const LEVELS' in t and t.count('par:') >= 1 else 1)",
]

_HEURISTIC_STOPS = frozenset({
    "no_progress", "not_converging", "gate_not_improving", "planning_churn",
    "dispatch_wedged", "revise_livelock", "completion_blocked",
    "worker_unproductive", "member_unhealthy", "delivery_review_stalled",
    "hard_blocker", "no_actionable_work",
})


def _green_probe(url, frames, *, screenshot_path="", timeout_ms=15000):
    assert url.startswith("http://127.0.0.1:")
    return {"ok": True, "non_black": True, "console_errors": [],
            "reason": "canvas painted", "screenshot": screenshot_path}


def _black_probe(url, frames, *, screenshot_path="", timeout_ms=15000):
    return {"ok": False, "non_black": False, "console_errors": [],
            "reason": "frame is uniformly black (mean=0.0, var=0.0)",
            "screenshot": screenshot_path}


class RunFixture:
    """A completed run, plus the readers the assertions use."""

    def __init__(self, store: LedgerStore, runner: CodingRunner,
                 team: ScriptedTeam, counters: TracingCounters, result: Any) -> None:
        self.store = store
        self.runner = runner
        self.team = team
        self.traced = counters
        self.res = result

    @property
    def counters(self) -> Any:
        return self.res.counters

    def choices(self) -> list[str]:
        return [d.get("choice") for d in self.store.list_decisions()]

    def decision_titles(self) -> list[str]:
        return [str(d.get("title") or "") for d in self.store.list_decisions()]

    def probe_runs(self) -> list[dict[str, Any]]:
        return [r for r in self.store.list_test_runs()
                if list(r.get("command_ids") or []) == [web_probe.PROBE_COMMAND_ID]]

    def delivered_head(self) -> str:
        return str(self.store.get_run_state().get("delivery_reviewed_head", "") or "")

    def master_files(self) -> list[str]:
        return [f for f in self.runner.workspace.list_files(scope="master")
                if f != ".gitignore"]

    def read_master(self, rel: str) -> str:
        return self.runner.workspace._ws.read_file(rel)


def _run_fixture(project_id: str, monkeypatch: Any, *, probe=_green_probe,
                 friction: bool = True, claim_done: bool = True,
                 register_gate: bool = False,
                 max_parallel_workers: Optional[int] = 1,
                 strict_file_partition: bool = True,
                 max_iterations: int = 80,
                 patch_probe: bool = True) -> RunFixture:
    store = LedgerStore(project_id)
    store.create_project(north_star=NORTH_STAR, definition_of_done=DOD,
                         target="new", repo_path=None)
    if register_gate:
        # Item 2 variant B. Pre-registered on purpose: `gate_bootstrap`'s one-shot
        # memo means a test file that appears on master LATER never arms the gate,
        # and a `sys.executable` argv avoids bootstrap's bare "python" argv and its
        # host-PATH fragility.
        store.set_test_commands({"acceptance": {
            "argv": _ACCEPTANCE_ARGV, "cwd": ".", "timeout_seconds": 60,
            "label": "acceptance (level data)", "scope": "acceptance"}})
    if patch_probe:
        monkeypatch.setattr(web_probe, "_default_node_runner", probe)
    policy = CodingAutonomyPolicy(
        checkpoint_cadence=CADENCE_OFF,
        max_iterations=max_iterations,
        # Run the in-loop gate — and therefore the web probe — after EVERY
        # gate-relevant merge, so a gate signal exists early enough for L1's "a green
        # gate held for >= 12 iterations" to mean something.
        gate_min_merge_interval=1,
        max_parallel_workers=max_parallel_workers,
        strict_file_partition=strict_file_partition,
    )
    # The runner-side arming/bootstrap paths read the PERSISTED policy
    # (`load_policy(store)`), exactly as the product route does, so these knobs have
    # to be saved rather than only passed to `run`.
    autonomy.save_policy(store, policy)
    team = ScriptedTeam(store, friction=friction, claim_done=claim_done)
    runner = CodingRunner(project_id, MEMBERS, team, guardrail_enabled=True)
    counters = TracingCounters()
    res = runner.run(policy, counters=counters)
    return RunFixture(store, runner, team, counters, res)


@pytest.fixture(scope="module")
def variant_a(tmp_path_factory: pytest.TempPathFactory):
    """Variant A — registry EMPTY (the gravity-golf configuration): no test command
    is registered and `gate_bootstrap` proposes none, so `_run_gate` executes nothing
    and **the web probe is the only acceptance signal in the entire run**.

    Module-scoped because the core assertions, the Item 5 budget, the Item 3 friction
    meta-tests and the Item 6 locks are all statements about ONE run; the fixture
    pins its own hermetic ``$ERRORTA_HOME`` (mirroring the suite conftest) so the
    sharing costs no isolation."""
    mp = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("spec28-variant-a")
    home = root / ".errorta"
    home.mkdir(parents=True, exist_ok=True)
    mp.setenv("ERRORTA_HOME", str(home))
    mp.setenv("HOME", str(root))
    mp.setenv("USERPROFILE", str(root))
    try:
        yield _run_fixture("spec28-a", mp)
    finally:
        mp.undo()


# --------------------------------------------------------------------------- #
# Item 2 — the core acceptance assertions
# --------------------------------------------------------------------------- #
def _assert_artifact_graph_on_master(fx: RunFixture) -> None:
    """Assertion 3: `index.html` and EVERY path in its `<script src>` graph exist on
    master — Spec 13's own foundation predicate, asserted on the artifact instead of
    on the detector, and one level deep into each module's relative imports."""
    files = set(fx.master_files())
    assert "index.html" in files, files
    srcs = re.findall(r"""<script\b[^>]*\bsrc\s*=\s*['"]([^'"]+)['"]""",
                      fx.read_master("index.html"))
    assert srcs, "index.html carries no <script src> graph"
    for src in srcs:
        assert src in files, f"{src} is referenced by index.html but absent on master"
        body = fx.read_master(src)
        for spec in re.findall(r"""\bfrom\s*['"]([^'"]+)['"]""", body):
            assert spec.startswith("."), f"{src} imports the bare specifier {spec!r}"
            target = f"src/{spec.lstrip('./')}"
            assert target in files, f"{src} imports {target}, absent on master"


def test_tier1_variant_a_reaches_definition_of_done(variant_a: RunFixture) -> None:
    """Item 2 assertions 1-4 on the registry-EMPTY configuration."""
    fx = variant_a
    assert fx.team.unexpected == [], fx.team.unexpected
    assert fx.res.stop_reason == DEFINITION_OF_DONE, fx.res.detail
    project = fx.store.get_project()
    assert project.status == "done"
    assert project.completion_summary.strip()
    assert project.completed_at
    # The registry really was empty — the probe was the ONLY acceptance signal.
    assert fx.store.get_test_commands() == {}
    _assert_artifact_graph_on_master(fx)
    # Assertion 4: the GL01 probe arm was REACHED BY A REAL RUN, and bound to the
    # delivered head. Before Spec 28 no test drove `CodingRunner` against a web
    # project at all, so this row had never existed outside a hand-driven call.
    probes = fx.probe_runs()
    assert probes, "no web:probe run was recorded by the loop"
    assert all(r["command_ids"] == [web_probe.PROBE_COMMAND_ID] for r in probes)
    assert all(r["passed"] for r in probes)
    delivered = fx.delivered_head()
    assert delivered
    assert any(str(r.get("head") or "") == delivered for r in probes), (
        [r.get("head") for r in probes], delivered)


def test_tier1_variant_b_registry_armed(monkeypatch: pytest.MonkeyPatch,
                                        tmp_errorta_home: Path) -> None:
    """Variant B — the command gate and the probe arm COEXIST, and both are green on
    the delivered head (Item 2 assertions 1-4 + 6)."""
    fx = _run_fixture("spec28-b", monkeypatch, register_gate=True)
    assert fx.team.unexpected == [], fx.team.unexpected
    assert fx.res.stop_reason == DEFINITION_OF_DONE, fx.res.detail
    assert fx.store.get_project().status == "done"
    _assert_artifact_graph_on_master(fx)
    delivered = fx.delivered_head()
    assert delivered
    probes = [r for r in fx.probe_runs() if str(r.get("head") or "") == delivered]
    assert probes and all(r["passed"] for r in probes)
    # Assertion 6: the recorded ACCEPTANCE session passed, at the delivered head.
    sessions = [r for r in fx.store.list_test_runs()
                if "acceptance" in (r.get("command_ids") or [])]
    assert sessions, "the registered acceptance command never ran"
    delivered_sessions = [r for r in sessions if str(r.get("head") or "") == delivered]
    assert delivered_sessions, [r.get("head") for r in sessions]
    assert all(r["passed"] for r in delivered_sessions)


# --------------------------------------------------------------------------- #
# Item 5 — the stop-reason budget
# --------------------------------------------------------------------------- #
def test_tier1_stop_reason_budget(variant_a: RunFixture) -> None:
    """B1-B9: not just THAT it finished but HOW. Each line is one of the roadmap's
    "how we will know it worked" criteria, made executable."""
    fx = variant_a
    c = fx.counters
    choices = fx.choices()

    # B1 + B2: finished on the definition of done, and no heuristic detector even
    # tripped. Post-SPEC-23 a heuristic condition becomes a LAST WORD before it
    # becomes a stop, so "no intervention was requested" is the stronger statement
    # and it subsumes "did not stop on one".
    assert fx.res.stop_reason == DEFINITION_OF_DONE
    assert fx.res.stop_reason not in _HEURISTIC_STOPS
    assert "last_word_requested" not in choices, [
        d for d in fx.store.list_decisions()
        if d.get("choice") == "last_word_requested"]
    assert fx.team.last_word_prompts == []

    # B3: the revise chain stayed shallow and was never broken.
    assert "revise_chain_broken" not in choices
    limit = CodingAutonomyPolicy().revise_chain_limit
    depths = [int((t._extras or {}).get("revise_depth") or 0)
              for t in fx.store.list_tasks()]
    assert max(depths) <= limit, depths

    # B4: the context channel was used and never saturated.
    assert "context_request_exhausted" not in choices
    attempts = [_context_attempts(t) for t in fx.store.list_tasks()]
    assert max(attempts) < 3, attempts

    # B5: PR economics inside the healthy band (run 1 was 53/96 superseded at a 30%
    # merge rate; GL04's clamp trips at ratio >= 0.5 / merge-rate <= 0.35).
    prs = fx.store.list_prs()
    superseded = sum(1 for p in prs if p.get("status") == "superseded")
    merged = sum(1 for p in prs if p.get("status") == "merged")
    assert superseded <= 1, [p.get("status") for p in prs]
    assert merged / len(prs) >= 0.8, [p.get("status") for p in prs]

    # B6: finished well inside its budget, not by nearly exhausting it.
    assert c.iterations <= 60, c.iterations

    # B7: every scripted envelope was legal by construction — a repair would mean the
    # parser is repairing something this fixture asserts is valid (a Spec 25 finding
    # surfacing as a red test, which is exactly the point).
    assert c.turns_repaired == 0

    # B8: the F127 recovery ladder is a recovery path; a healthy run never needs it.
    assert c.model_escalations == 0
    assert c.task_reassignments == 0
    assert c.pm_assists == 0

    # B9: the foundation LANDED; it did not stall.
    assert "foundation_not_converging" not in choices


# --------------------------------------------------------------------------- #
# Item 3 — the friction meta-tests: a transcript that cannot go soft
# --------------------------------------------------------------------------- #
def test_f1_a_review_was_rejected_and_revised(variant_a: RunFixture) -> None:
    fx = variant_a
    assert "review_rejected" in fx.choices()
    rejected = [p for p in fx.store.list_prs() if p.get("reviewer_approved") is False]
    assert rejected, [p.get("status") for p in fx.store.list_prs()]
    # The rejected PR ends SUPERSEDED, which is what a revise landing looks like.
    assert all(p.get("status") == "superseded" for p in rejected)
    revises = [t for t in fx.store.list_tasks() if t.title.startswith("revise:")]
    assert revises, [t.title for t in fx.store.list_tasks()]
    assert all(t.state == "done" for t in revises), [t.state for t in revises]


def test_f2_a_duplicate_task_was_rejected_and_charged_idle(
    variant_a: RunFixture,
) -> None:
    fx = variant_a
    assert "duplicate_task_rejected" in fx.choices()
    # The rejection re-armed the idle detector, which is the behaviour Spec 08
    # depends on and the state L2 needs to exist before the prune turn can matter.
    assert fx.traced.max_pm_idle >= 1
    assert fx.traced.max_pm_idle < CodingAutonomyPolicy().pm_idle_limit


def test_f3_the_spec21_prune_turn_parsed_and_counted_as_progress(
    variant_a: RunFixture,
) -> None:
    """The exact envelope that locked run 3 out: `{"kind":"plan","done":false,
    "tasks":[],"decisions":[{"decision":...}]}` — no `title` key, no tasks, not done.
    It must parse, and it must be credited as progress."""
    fx = variant_a
    assert PRUNE_DECISION in fx.decision_titles()
    assert "pm_turn_rejected" not in fx.choices()
    # EXACTLY one PM turn in the whole run was charged as idle — the all-duplicate
    # batch (F2). If the decision-only prune turn had been charged too this would be
    # 2. That is the P0.5 half of Spec 21, asserted at LOOP level.
    assert fx.traced.pm_idle_charges == 1, fx.traced.pm_idle_charges


def test_f4_a_context_request_was_answered_and_did_not_saturate(
    variant_a: RunFixture,
) -> None:
    fx = variant_a
    assert fx.team.context_asks == 1
    assert "context_request" in fx.choices()
    asked = [t for t in fx.store.list_tasks() if _context_attempts(t) > 0]
    assert len(asked) == 1, [t.title for t in asked]
    assert _context_attempts(asked[0]) == 1


def test_f5_the_run_was_long_enough_for_the_detectors_to_be_live(
    variant_a: RunFixture,
) -> None:
    """Without this the green-gate lock (L1) is vacuous — and vacuous is precisely
    how `test_coding_runner.py`'s existing end-to-end test misses it today."""
    assert variant_a.counters.iterations >= 12, variant_a.counters.iterations


# --------------------------------------------------------------------------- #
# Item 6 — loop-level locks for the three known false stops
# --------------------------------------------------------------------------- #
def test_l1_a_green_gate_does_not_stop_a_healthy_run(variant_a: RunFixture) -> None:
    """Run 1 died `gate_not_improving` at iteration 22 with 6/6 PRs merged. Spec 21
    proved `_account_gate_stall` returns None for a hand-built GREEN `_Ledger` stub;
    this proves the REAL green signal — a probe verdict recorded by
    `web_probe.run_and_record`, read through `_gate_fingerprint` into
    `c.last_gate_best` — behaves the same once the window has had room to trip."""
    fx = variant_a
    probes = fx.probe_runs()
    assert len(probes) >= 2 and all(r["passed"] for r in probes)
    first_gate = fx.traced.first_gate_iter
    assert first_gate >= 0, "no gate signal ever reached the loop counters"
    held = fx.counters.iterations - first_gate
    assert held >= 12, (first_gate, fx.counters.iterations)
    assert held >= CodingAutonomyPolicy().gate_stall_limit
    assert "gate_not_improving" not in fx.choices()
    assert fx.res.stop_reason == DEFINITION_OF_DONE


def test_l2_the_pm_schema_lockout_cannot_recur(variant_a: RunFixture) -> None:
    """Run 2 stopped `no_progress` with two PRs open after four rejected PM turns.
    Spec 21 proves the VALIDATOR accepts the prune envelope; this proves the LOOP
    credits it — a different claim, and the one that was false."""
    fx = variant_a
    assert fx.res.stop_reason != "no_progress"
    assert fx.traced.max_pm_idle < CodingAutonomyPolicy().pm_idle_limit
    assert fx.counters.pm_idle == 0
    assert "pm_turn_rejected" not in fx.choices()


def test_l3_the_decision_field_synonym_survives_the_full_path(
    variant_a: RunFixture,
) -> None:
    """`{"decision": ...}` with no `title` — run 2 lost three of four PM retries to
    `missing decisions[0].title`. Asserted after `parse_coding_turn` ->
    `_materialize_pm_tasks` -> `_apply_outcome`, on the recorded decision."""
    fx = variant_a
    recorded = [d for d in fx.store.list_decisions()
                if str(d.get("title") or "") == PRUNE_DECISION]
    assert recorded, fx.decision_titles()
    assert recorded[0].get("choice") == "pm_decision"
    assert "duplicate" in str(recorded[0].get("rationale") or "").lower()


# --------------------------------------------------------------------------- #
# The negative controls — without these, a disabled probe arm or a defaulted
# assertion passes just as happily as a working one
# --------------------------------------------------------------------------- #
def test_a_red_probe_verdict_prevents_definition_of_done(
    monkeypatch: pytest.MonkeyPatch, tmp_errorta_home: Path,
) -> None:
    """Item 2 assertion 5. The probe verdict must be LOAD-BEARING. A black frame is
    the P2 defect the 2026-07-24 artifact shipped with — clean console, initialized
    game state, pure black screen — and no other signal in the pipeline can see it."""
    fx = _run_fixture("spec28-red", monkeypatch, probe=_black_probe)
    assert fx.res.stop_reason != DEFINITION_OF_DONE, fx.res.detail
    assert fx.store.get_project().status != "done"
    probes = fx.probe_runs()
    assert probes and not any(r["passed"] for r in probes)
    # The verdict text rides the record VERBATIM, so a human sees why.
    assert any("black" in str(r.get("results")).lower() for r in probes)


def test_a_pm_that_never_claims_done_does_not_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_errorta_home: Path,
) -> None:
    """The second negative control: the DoD assertion must be reading the real
    completion path, not defaulting to it. A PM that keeps recording honest decisions
    and never sets `done=true` must end on a stop reason, never on the definition of
    done."""
    fx = _run_fixture("spec28-nodone", monkeypatch, claim_done=False,
                      max_iterations=30)
    assert fx.res.stop_reason != DEFINITION_OF_DONE
    assert fx.store.get_project().status != "done"


# --------------------------------------------------------------------------- #
# Both loop chains (batch-plan regression lock 5)
# --------------------------------------------------------------------------- #
def test_tier1_concurrent_fanout_completes(monkeypatch: pytest.MonkeyPatch,
                                           tmp_errorta_home: Path) -> None:
    """The SAME transcript on the concurrent chain (`plan_next_batch` /
    `_run_concurrent_loop`), where real fanned-out runs live.

    `strict_file_partition=False` is not a convenience: with it ON this run cannot
    finish, for the reason pinned by
    :func:`test_concurrent_fanout_wedges_a_path_citing_revise` below. Everything else
    is the default policy."""
    fx = _run_fixture("spec28-fanout", monkeypatch, max_parallel_workers=None,
                      strict_file_partition=False)
    assert fx.team.unexpected == [], fx.team.unexpected
    assert fx.res.stop_reason == DEFINITION_OF_DONE, fx.res.detail
    assert fx.store.get_project().status == "done"
    _assert_artifact_graph_on_master(fx)
    assert any(r["passed"] for r in fx.probe_runs())
    assert fx.counters.iterations >= 12


def test_concurrent_fanout_wedges_a_path_citing_revise(
    monkeypatch: pytest.MonkeyPatch, tmp_errorta_home: Path,
) -> None:
    """DEFECT LOCK, not an endorsement — the composition bug Spec 28 found.

    GL05's strict a-priori file-ownership partition holds a path from the first tick
    an in-flight DEV task owns it, where "in-flight" includes *any task carrying a
    non-terminal PR* (`autonomy.inflight_owned_paths`). A reviewer's CITED blocking
    finding spawns a `revise:` task whose detail names that same path, while the PR
    the revise supersedes stays `changes_requested` — a live PR — until that very
    revise merges. So `plan_next_batch` skips the revise on every tick, forever: the
    run plans, plans, plans and stops `no_progress` with the revise still `todo` and
    `next_task("dev")` happily returning it.

    THE FIX (out of scope for Spec 28, which changes no engine code): the partition
    must not let a PR's own revise lineage be blocked by the PR it supersedes — e.g.
    exclude the task behind `task.pr_id` from the owner set when scoring that task,
    the way `_materialize_pm_tasks` already excludes a PMAssist parent from its own
    dedupe index. WHEN THAT LANDS this test goes red: delete it, and drop the
    `strict_file_partition=False` from `test_tier1_concurrent_fanout_completes`."""
    fx = _run_fixture("spec28-wedge", monkeypatch, max_parallel_workers=None,
                      strict_file_partition=True, max_iterations=40)
    assert fx.res.stop_reason != DEFINITION_OF_DONE
    stuck = [t for t in fx.store.list_tasks()
             if t.title.startswith("revise:") and t.state == "todo"]
    assert stuck, [(t.title, t.state) for t in fx.store.list_tasks()]
    # The scheduler would hand it out — only the partition holds it back.
    assert fx.store.next_task("dev") is not None
    assert fx.store.get_pr(stuck[0].pr_id)["status"] == "changes_requested"


# --------------------------------------------------------------------------- #
# Tier 1b — the browser-backed tier (gates when the toolchain is present)
# --------------------------------------------------------------------------- #
_PROBE_SCRIPT = _REPO_ROOT / "scripts" / "web-probe.mjs"


@functools.lru_cache(maxsize=1)
def _playwright_available() -> bool:
    """The predicate `test_gl01_node_probe_smoke.py` already uses: node, the probe
    script, a Playwright package, and a reachable Chromium binary. Memoized because
    it spawns a node subprocess and both this file's skipif and Tier 2's ask for
    it at COLLECTION time."""
    if shutil.which("node") is None or not _PROBE_SCRIPT.exists():
        return False
    check = ("Promise.any(['@playwright/test','playwright','playwright-core']"
             ".map(m=>import(m).then(x=>{if(!x.chromium)throw 0;return x.chromium"
             ".executablePath()})) ).then(async p=>{const fs=await import('node:fs');"
             "fs.accessSync(p);process.exit(0)}).catch(()=>process.exit(1))")
    try:
        proc = subprocess.run(["node", "--input-type=module", "-e", check],
                              cwd=str(_REPO_ROOT), capture_output=True,
                              timeout=60, check=False)
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(
    not _playwright_available(),
    reason="node + Playwright + a Chromium browser are required for the browser tier")
def test_tier1b_real_chromium_probe_drives_the_delivered_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_errorta_home: Path,
) -> None:
    """The same fixture with `_default_node_runner` NOT patched, so the real
    `scripts/web-probe.mjs` drives real Chromium against the real served tree. This
    is the only tier that asserts PIXELS: the canvas is non-black and the console is
    clean, on an artifact a run produced by itself."""
    fx = _run_fixture("spec28-browser", monkeypatch, patch_probe=False)
    assert fx.res.stop_reason == DEFINITION_OF_DONE, fx.res.detail
    _assert_artifact_graph_on_master(fx)
    delivered = fx.delivered_head()
    probes = [r for r in fx.probe_runs() if str(r.get("head") or "") == delivered]
    assert probes, [r.get("head") for r in fx.probe_runs()]
    assert all(r["passed"] for r in probes)
    stamped = [p for p in fx.store.list_prs() if p.get("probe_head")]
    assert any(p.get("probe_non_black") is True and p.get("probe_console_errors") == 0
               for p in stamped), [(p.get("probe_non_black"),
                                    p.get("probe_console_errors")) for p in stamped]


# --------------------------------------------------------------------------- #
# The marker lock (Item 7's load-bearing prerequisite)
# --------------------------------------------------------------------------- #
def test_pyproject_addopts_deselects_live_flaky_and_manual() -> None:
    """Anti-drift canary, in the established `test_f145_pm_reference.py` style.
    Without `addopts` a `live` test spends real money inside `( cd python && pytest )`
    on every PR, and the `flaky` marker's declared contract — "never part of the merge
    gate" — is aspirational rather than true. One line makes both real; this locks it
    so it cannot be deleted by anyone editing pytest config."""
    import tomllib

    with (_REPO_ROOT / "python" / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)
    ini = cfg["tool"]["pytest"]["ini_options"]
    addopts = ini["addopts"]
    assert "-m" in addopts
    for marker in ("live", "flaky", "manual"):
        assert f"not {marker}" in addopts, addopts
        # The marker must also still be DECLARED, or `-m` would filter on a typo.
        assert any(m.split(":", 1)[0] == marker for m in ini["markers"]), marker

