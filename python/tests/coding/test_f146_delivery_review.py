"""F146 Slice B — delivery review of the INTEGRATED delivered head.

The team works branch-per-task (F087-17); each PR is reviewed/tested at its OWN
head, but the final merges produce new integration commits nothing signed off on.
Slice B runs a real delivery review (a reviewer over the whole delivered diff +
the registered test suite) bound to ``workspace.head()`` at the ``project_done``
transition, fail-closed: a reject / test failure blocks ``done`` and re-opens the
run; a clean pass binds a reviewer verdict + a test run to the exact delivered
head so the merge-back accept gate no longer shows ``unreviewed_changes`` /
``tests_missing`` on a finished project.
"""
import json
import re
import sys
from pathlib import Path

from errorta_council.coding.autonomy import (
    CADENCE_OFF,
    DEFINITION_OF_DONE,
    CodingAutonomyPolicy,
)
from errorta_council.coding.evidence import merge_review
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    CodingRunner,
    build_run_turn,
    members_by_coding_role,
)

MEMBERS = [
    {"id": "m-pm", "enabled": True, "metadata": {"coding_role": "pm"}},
    {"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}},
    {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}},
    {"id": "m-test", "enabled": True, "metadata": {"coding_role": "tester"}},
]

_ADD = "def add(a, b):\n    return a + b\n"


def _task_id(prompt: str, role: str) -> str:
    return re.search(rf"{role} for task id '([^']+)'", prompt).group(1)


def _pr_head(prompt: str) -> str:
    return re.search(r"PR head you are reviewing is '([^']*)'", prompt).group(1)


def _delivery_head(prompt: str) -> str:
    return re.search(r"delivered head you are reviewing is '([^']*)'", prompt).group(1)


def _pm_env(*, tasks=None, done=False, completion_summary="") -> str:
    intent = {"kind": "plan", "done": done}
    if tasks is not None:
        intent["tasks"] = tasks
    if completion_summary:
        intent["completion_summary"] = completion_summary
    return json.dumps({"schema_version": "coding_turn.v1", "role": "pm", "intent": intent})


def _dev_env(task_id: str, files) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "dev", "task_id": task_id,
        "intent": {"kind": "tool_plan", "task_type": "implementation",
                   "tool_calls": [{"tool": "code_write",
                                   "args": {"path": p, "content": c}}
                                  for p, c in files]}})


def _rev_env(task_id: str, head: str, *, approved=True, findings=None) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer", "task_id": task_id,
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": findings or []}})


def _tester_env(task_id: str, command_ids) -> str:
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "tester", "task_id": task_id,
        "intent": {"kind": "test_plan", "command_ids": command_ids,
                   "scope": "full_project", "rationale": "run"}})


class DeliveryFake:
    """One dev task (implement add), then the PM claims done. The delivery review
    verdict is configurable so the fail-closed path is exercised."""

    def __init__(self, *, deliver_approved: bool = True) -> None:
        self.pm_calls = 0
        self.deliver_approved = deliver_approved
        self.delivery_review_calls = 0

    def __call__(self, member: dict, prompt: str) -> str:
        if "DELIVERY reviewer" in prompt:
            self.delivery_review_calls += 1
            head = _delivery_head(prompt)
            if self.deliver_approved:
                return _rev_env("delivery-review", head, approved=True)
            return _rev_env(
                "delivery-review", head, approved=False,
                findings=[{"severity": "blocking",
                           "title": "integration defect",
                           "body": "the delivered code is broken as assembled"}])
        if "You are the PM" in prompt:
            self.pm_calls += 1
            if self.pm_calls == 1:
                return _pm_env(tasks=[{"title": "implement add", "role": "dev"}])
            return _pm_env(done=True, completion_summary="add done")
        if "You are a developer" in prompt:
            return _dev_env(_task_id(prompt, "developer"), [("calc.py", _ADD)])
        if "You are a reviewer" in prompt:
            return _rev_env(_task_id(prompt, "reviewer"), _pr_head(prompt), approved=True)
        if "You are a tester" in prompt:
            return _tester_env(_task_id(prompt, "tester"), ["unit"])
        return "{}"


_PASS_CMD = {"unit": {
    "argv": [sys.executable, "-c",
             "import sys; sys.path.insert(0,'.'); from calc import add; "
             "assert add(1,2)==3"],
    "cwd": ".", "timeout_seconds": 30}}
# Same shape, but the assertion always fails -> the suite fails against the head.
_FAIL_CMD = {"unit": {
    "argv": [sys.executable, "-c", "import sys; sys.exit(1)"],
    "cwd": ".", "timeout_seconds": 30}}


def _make(pid: str, cmds: dict) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="calc with add",
                         definition_of_done="add works + tested",
                         target="new", repo_path=None)
    if cmds:
        store.set_test_commands(cmds)
    return store


def test_delivery_review_binds_verdict_to_delivered_head(tmp_errorta_home: Path) -> None:
    # Acceptance #1: after done, the merge-back gate for the delivered head has
    # NO unreviewed_changes and NO tests_missing — a reviewer verdict AND a test
    # run are bound to that exact integrated head.
    store = _make("f146-ok", _PASS_CMD)
    fake = DeliveryFake(deliver_approved=True)
    runner = CodingRunner("f146-ok", MEMBERS, fake, guardrail_enabled=True)
    res = runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF,
                                          max_iterations=40))
    assert res.stop_reason == DEFINITION_OF_DONE
    assert store.get_project().status == "done"
    head = runner.workspace.head()
    # A delivery review actually ran against the delivered head.
    assert fake.delivery_review_calls >= 1
    rs = store.get_run_state()
    assert rs.get("delivery_reviewed_head") == head
    assert rs.get("delivery_review_passed") is True
    # The gate for the delivered head is clean of the head-binding blockers.
    codes = {b["code"] for b in merge_review(store, runner.workspace)["gate"]["blockers"]}
    assert "unreviewed_changes" not in codes
    assert "tests_missing" not in codes


def test_delivery_review_reject_blocks_done_and_reopens(tmp_errorta_home: Path) -> None:
    # Acceptance #2: a rejected delivery review does NOT mark done; the failure is
    # filed as dev work so the run re-opens (Slice E path).
    store = _make("f146-reject", _PASS_CMD)
    fake = DeliveryFake(deliver_approved=False)
    runner = CodingRunner("f146-reject", MEMBERS, fake, guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=40))
    assert store.get_project().status != "done"
    titles = [t.title for t in store.list_tasks()]
    assert any("delivery review" in t.lower() for t in titles), titles
    rs = store.get_run_state()
    assert rs.get("delivery_review_passed") is False


def test_delivery_tests_fail_block_done(tmp_errorta_home: Path) -> None:
    # Acceptance #3 (test side): the registered delivery suite fails against the
    # delivered head -> the verifier returns not-passed, records a failed delivery
    # test run bound to the head, and files a "fix delivery tests" dev task.
    #
    # Isolate the DELIVERY test run from the per-PR tester: complete a run with no
    # test commands (so the PR flow lands on review-approval alone), THEN register
    # a failing command, bust the once-per-head cache, and invoke the verifier
    # directly on the populated workspace.
    store = _make("f146-testfail", {})
    runner = CodingRunner("f146-testfail", MEMBERS, DeliveryFake(deliver_approved=True),
                          guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=40))
    store.set_test_commands(_FAIL_CMD)
    store.set_run_state(delivery_reviewed_head="__stale__")  # force a re-review

    def approve_caller(member: dict, prompt: str) -> str:
        return _rev_env("delivery-review", _delivery_head(prompt), approved=True)

    rt = build_run_turn(store, runner.workspace,
                        members_by_coding_role(MEMBERS), approve_caller,
                        guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed is False
    delivery_runs = [r for r in store.list_test_runs()
                     if r.get("task_id") == "delivery-review"]
    assert delivery_runs and not any(r.get("passed") for r in delivery_runs)
    assert any(t.title == "fix delivery tests" for t in store.list_tasks())


def test_delivery_review_cached_once_per_head(tmp_errorta_home: Path) -> None:
    # Acceptance #6: delivery review runs at most once per unchanged delivered
    # head. After a completed run, re-invoking the verifier at the same head is a
    # cache hit — no new reviewer call.
    store = _make("f146-cache", _PASS_CMD)
    fake = DeliveryFake(deliver_approved=True)
    runner = CodingRunner("f146-cache", MEMBERS, fake, guardrail_enabled=True)
    runner.run(CodingAutonomyPolicy(checkpoint_cadence=CADENCE_OFF, max_iterations=40))
    calls: list[str] = []

    def counting_caller(member: dict, prompt: str) -> str:
        calls.append(prompt)
        return _rev_env("delivery-review", _delivery_head(prompt), approved=True)

    rt = build_run_turn(store, runner.workspace,
                        members_by_coding_role(MEMBERS), counting_caller,
                        guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed and result.reason == "cached"
    assert calls == []  # cache hit -> the reviewer was NOT called again


class _BrokenHeadWs:
    """A REAL workspace (exists) whose head() surfaces a git error as "" — the
    F146 fail-closed edge: an inability to bind a verdict to a head must block."""
    def head(self) -> str:
        return ""

    def exists(self) -> bool:
        return True

    def root(self):  # pragma: no cover - not reached (blocks before tests)
        raise AssertionError("root() must not be reached on a head error")

    def preview(self):  # pragma: no cover - not reached
        raise AssertionError("preview() must not be reached on a head error")


class _BrokenPreviewWs:
    """A real workspace with a head whose preview() raises (corrupt worktree,
    F087-15 M1) — must block done rather than review a blank diff."""
    def head(self) -> str:
        return "abc123def456"

    def exists(self) -> bool:
        return True

    def root(self):  # pragma: no cover - not reached
        raise AssertionError("root() must not be reached on a preview error")

    def preview(self):
        raise RuntimeError("worktree is missing")


def test_delivery_review_blocks_on_head_error(tmp_errorta_home: Path) -> None:
    # MEDIUM (adversarial review): a transient git error at head() on a real
    # workspace must NOT mark done — it is a verify error, fail-closed.
    store = _make("f146-headerr", {})
    calls: list[str] = []

    def caller(member: dict, prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    rt = build_run_turn(store, _BrokenHeadWs(),
                        members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed is False
    assert result.reason == "workspace head unavailable"
    assert calls == []  # never reached the reviewer


def test_delivery_review_blocks_on_preview_error(tmp_errorta_home: Path) -> None:
    # MEDIUM (adversarial review): a corrupt/missing worktree (preview raises)
    # must block done instead of reviewing a blank diff and passing.
    store = _make("f146-previewerr", {})
    calls: list[str] = []

    def caller(member: dict, prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    rt = build_run_turn(store, _BrokenPreviewWs(),
                        members_by_coding_role(MEMBERS), caller,
                        guardrail_enabled=True)
    result = rt.delivery_review(store)
    assert result.passed is False
    assert result.reason == "delivered diff unavailable (preview failed)"
    assert calls == []  # never reviewed a blank diff


def test_delivery_review_skipped_without_reviewer_or_pm(tmp_errorta_home: Path) -> None:
    # A team with neither a reviewer nor a PM cannot run a real delivery review;
    # the verifier records NO verdict (the gate honestly stays unreviewed) and
    # preserves prior done behavior instead of blocking forever. Not a rubber-stamp.
    store = _make("f146-noreviewer", {})
    calls: list[str] = []

    def caller(member: dict, prompt: str) -> str:
        calls.append(prompt)
        return "{}"

    dev_only = [{"id": "m-dev", "enabled": True, "metadata": {"coding_role": "dev"}}]
    rt = build_run_turn(store, None, members_by_coding_role(dev_only), caller,
                        guardrail_enabled=True)
    # workspace=None -> nothing to verify; passes without any model call.
    result = rt.delivery_review(store)
    assert result.passed
    assert calls == []
