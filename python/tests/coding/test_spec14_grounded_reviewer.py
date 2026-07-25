"""Spec 14 (S3) — ground the reviewer.

The gravity-golf reviewer gave 6-second empty approvals (it could not see the
repo) and, when it did reject, demanded execution evidence nobody could produce.
This suite locks the fix:

* the read-only retrieval mechanism is role-neutral (Spec 11's dev path
  generalized), and a reviewer turn mounts the PR worktree;
* a blocking finding with no citable path is flagged `cited: false` WITHOUT
  changing the verdict (the fail-closed lock — an uncited finding never makes a PR
  mergeable, and never auto-suppresses the rejection here; Spec 15 owns that);
* an empty approval produced without reading (num_turns <= 1) is retried once and,
  if still ungrounded, ACCEPTED but flagged (never blocked — blocking would be a
  new wedge).
"""
import sys
from pathlib import Path

from errorta_council.coding import runner
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace

_PY = sys.executable


# --------------------------------------------------------------------------- #
# Phase 1 — the provider retrieval mechanism is role-neutral (canonical
# repo_read_root, legacy dev_repo_read_root still accepted).
# --------------------------------------------------------------------------- #


def test_provider_accepts_both_repo_read_keys() -> None:
    from types import SimpleNamespace

    from errorta_model_gateway.providers.async_claude_cli import _repo_read_root

    tmp = str(Path(__file__).parent)  # an existing dir
    canonical = SimpleNamespace(extra={"metadata": {"repo_read_root": tmp}})
    legacy = SimpleNamespace(extra={"metadata": {"dev_repo_read_root": tmp}})
    none_req = SimpleNamespace(extra={"metadata": {}})
    assert _repo_read_root(canonical) == tmp
    assert _repo_read_root(legacy) == tmp
    assert _repo_read_root(none_req) is None
    # A non-existent dir is refused (fail safe).
    bad = SimpleNamespace(extra={"metadata": {"repo_read_root": "/no/such/dir/xyz"}})
    assert _repo_read_root(bad) is None


def test_num_turns_flows_through_the_result_types() -> None:
    from errorta_council.gateway_local import LocalCouncilModelResult
    from errorta_model_gateway.providers.async_base import AsyncProviderResult

    a = AsyncProviderResult(
        content="x", provider_class="claude_cli", model="opus",
        input_tokens=1, output_tokens=1, duration_ms=5, num_turns=7)
    assert a.num_turns == 7
    lo = LocalCouncilModelResult(
        content="x", provider="claude_cli", provider_class="claude_cli",
        model="opus", input_tokens=1, output_tokens=1, duration_ms=5,
        raw_usage_available=True, num_turns=7)
    assert lo.num_turns == 7
    # Optional/additive: absent -> None, nothing else breaks.
    lo2 = LocalCouncilModelResult(
        content="x", provider="fake", provider_class="local", model="m",
        input_tokens=None, output_tokens=None, duration_ms=0,
        raw_usage_available=False)
    assert lo2.num_turns is None


# --------------------------------------------------------------------------- #
# Phase 3 — the `cited` flag. Fail-closed: verdict untouched.
# --------------------------------------------------------------------------- #


class _WS:
    """Minimal workspace stub: master + branch file sets for citation checks."""

    def __init__(self, changed, master):
        self._changed, self._master = list(changed), list(master)

    def changed_paths(self, branch, base="master"):
        return list(self._changed)

    def list_files(self, *, scope=None):
        return list(self._master)


def test_uncited_blocking_finding_is_flagged_not_rescored() -> None:
    ws = _WS(changed=["src/api.js"], master=["src/api.js", "index.html"])
    findings = [
        {"severity": "blocking", "title": "no evidence tests ran", "path": "",
         "blocking": True},                                            # uncited
        {"severity": "blocking", "title": "null deref", "path": "src/api.js",
         "blocking": True},                                            # cited
        {"severity": "minor", "title": "nit", "path": "", "blocking": False},
    ]
    out = runner._mark_finding_citations(findings, workspace=ws, pr={"branch": "b"})
    assert out[0]["cited"] is False and out[0]["severity"] == "blocking"
    assert out[1]["cited"] is True
    # Non-blocking finding is untouched (no cited key forced).
    assert "cited" not in out[2]


def test_finding_citing_a_file_not_in_the_tree_is_uncited() -> None:
    ws = _WS(changed=["src/api.js"], master=["src/api.js"])
    out = runner._mark_finding_citations(
        [{"severity": "blocking", "title": "x", "path": "src/ghost.js",
          "blocking": True}],
        workspace=ws, pr={"branch": "b"})
    assert out[0]["cited"] is False


def test_citation_check_degrades_open_when_tree_unreadable() -> None:
    class _Boom:
        def changed_paths(self, *a, **k):
            raise RuntimeError("no worktree")

        def list_files(self, *a, **k):
            raise RuntimeError("no worktree")

    # With no readable tree, a finding that carries a path is accepted (cited),
    # rather than everything being spuriously flagged uncited.
    out = runner._mark_finding_citations(
        [{"severity": "blocking", "title": "x", "path": "src/a.js",
          "blocking": True}],
        workspace=_Boom(), pr={"branch": "b"})
    assert out[0]["cited"] is True


# --------------------------------------------------------------------------- #
# Phase 4/5 — the grounding helpers.
# --------------------------------------------------------------------------- #


def test_is_empty_approval_shape() -> None:
    from types import SimpleNamespace

    from errorta_council.coding.runner import TurnParseError

    def _p(approved, findings, head="h1"):
        return SimpleNamespace(intent=SimpleNamespace(
            approved=approved, findings=findings, reviewed_head=head))

    assert runner._is_empty_approval(_p(True, []), "h1") is True
    assert runner._is_empty_approval(_p(True, ["x"]), "h1") is False   # has findings
    assert runner._is_empty_approval(_p(False, []), "h1") is False     # rejected
    assert runner._is_empty_approval(_p(True, [], "old"), "h1") is False  # stale
    err = TurnParseError.__new__(TurnParseError)
    assert runner._is_empty_approval(err, "h1") is False


def test_last_turn_grounding_reads_the_sink(monkeypatch) -> None:
    import threading
    sink = threading.local()
    sink.last = {"num_turns": 5, "duration_ms": 8200}
    monkeypatch.setattr(runner, "_usage_sink", sink)
    assert runner._last_turn_grounding() == (5, 8200)
    sink.last = None
    assert runner._last_turn_grounding() == (None, None)


# --------------------------------------------------------------------------- #
# Phase 2/5 — end to end through build_run_turn against a fake caller, so we can
# script num_turns and assert the verdict/retry/flagging behavior deterministically.
# --------------------------------------------------------------------------- #


def _store(pid: str, tmp_path: Path) -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target="new",
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _open_reviewed_pr(store, ws):
    """Land a dev PR ready for review: a task, a branch with a file, a recorded PR."""
    dev_task = store.add_task(title="add module", role="dev")
    branch = ws.start_task_branch(dev_task.task_id)
    ws.write_file("src/mod.js", "export const x = 1\n", task_id=dev_task.task_id)
    pr = store.record_pr(task_id=dev_task.task_id, branch=branch,
                         head=ws.branch_head(branch), dev_member="m-dev")
    review_task = store.add_task(title=f"review PR: {dev_task.title}",
                                 role="reviewer", pr_id=pr["pr_id"],
                                 depends_on=[dev_task.task_id])
    return pr, review_task


def _reviewer_members():
    return {"reviewer": [{"id": "m-rev", "gateway_route_id": "claude_cli.opus",
                          "provider_kind": "claude_cli", "role": "reviewer"}]}


def test_empty_ungrounded_approval_is_retried_then_flagged(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    import json
    import threading

    from errorta_council.coding.topology import Assign

    s = _store("gr1", tmp_path)
    ws = _ws("gr1", s)
    pr, review_task = _open_reviewed_pr(s, ws)

    calls = {"n": 0}
    sink = threading.local()

    def caller(member, prompt):
        calls["n"] += 1
        # The reviewer turn mounted the worktree (repo_read_root present).
        assert member.get("repo_read_root")
        # num_turns == 1 both times -> ungrounded reflex, twice.
        sink.last = {"num_turns": 1, "duration_ms": 500}
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": True, "findings": []}})

    monkeypatch.setattr(runner, "_usage_sink", sink)
    rt = runner.build_run_turn(
        s, ws, _reviewer_members(), caller, guardrail_enabled=False,
        reviewer_repo_read=True)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role="reviewer"), s)

    assert calls["n"] == 2  # primary + one retry
    assert any(d["choice"] == "review_ungrounded" for d in s.list_decisions())
    # Accepted, not blocked: the verdict stuck as approved.
    assert s.get_pr(pr["pr_id"])["reviewer_approved"] is True
    assert s.get_pr(pr["pr_id"])["review_ungrounded"] is True


def test_grounded_approval_is_not_retried(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    import json
    import threading

    from errorta_council.coding.topology import Assign

    s = _store("gr2", tmp_path)
    ws = _ws("gr2", s)
    pr, review_task = _open_reviewed_pr(s, ws)
    calls = {"n": 0}
    sink = threading.local()

    def caller(member, prompt):
        calls["n"] += 1
        sink.last = {"num_turns": 6, "duration_ms": 9000}  # read the repo
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": True, "findings": []}})

    monkeypatch.setattr(runner, "_usage_sink", sink)
    rt = runner.build_run_turn(
        s, ws, _reviewer_members(), caller, guardrail_enabled=False,
        reviewer_repo_read=True)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role="reviewer"), s)

    assert calls["n"] == 1  # no retry
    assert not any(d["choice"] == "review_ungrounded" for d in s.list_decisions())
    assert s.get_pr(pr["pr_id"])["review_grounded"] is True


def test_reviewer_without_policy_does_not_mount_or_retry(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    import json
    import threading

    from errorta_council.coding.topology import Assign

    s = _store("gr3", tmp_path)
    ws = _ws("gr3", s)
    pr, review_task = _open_reviewed_pr(s, ws)
    calls = {"n": 0}
    sink = threading.local()

    def caller(member, prompt):
        calls["n"] += 1
        assert "repo_read_root" not in member  # not mounted
        sink.last = {"num_turns": 1, "duration_ms": 200}
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": True, "findings": []}})

    monkeypatch.setattr(runner, "_usage_sink", sink)
    rt = runner.build_run_turn(
        s, ws, _reviewer_members(), caller, guardrail_enabled=False,
        reviewer_repo_read=False)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role="reviewer"), s)

    # Retrieval off -> no mount, no retry (default review_min_latency_ms=0 disables
    # the latency fallback), no ungrounded flag.
    assert calls["n"] == 1
    assert not any(d["choice"] == "review_ungrounded" for d in s.list_decisions())


def _run_reviewer_no_num_turns(store, ws, monkeypatch, *, review_min_latency_ms):
    """Drive a reviewer turn whose provider does NOT report num_turns (a non-CLI /
    fake provider), returning a fast empty approval. Returns the caller call count."""
    import json
    import threading

    from errorta_council.coding.topology import Assign

    pr, review_task = _open_reviewed_pr(store, ws)
    calls = {"n": 0}
    sink = threading.local()

    def caller(member, prompt):
        calls["n"] += 1
        # num_turns absent (vendor doesn't report it), fast wall time.
        sink.last = {"num_turns": None, "duration_ms": 200}
        return json.dumps({
            "schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": True, "findings": []}})

    monkeypatch.setattr(runner, "_usage_sink", sink)
    rt = runner.build_run_turn(
        store, ws, _reviewer_members(), caller, guardrail_enabled=False,
        reviewer_repo_read=True, review_min_latency_ms=review_min_latency_ms)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role="reviewer"), store)
    return calls["n"], pr


def test_no_num_turns_fast_approval_not_retried_by_default(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """Item 4 false-positive lock: with the default review_min_latency_ms == 0, a
    fast empty approval from a provider that does not report num_turns is NEVER
    retried on latency alone — the branch is dead by default."""
    s = _store("gr4", tmp_path)
    ws = _ws("gr4", s)
    n, pr = _run_reviewer_no_num_turns(s, ws, monkeypatch, review_min_latency_ms=0)
    assert n == 1  # no retry
    assert not any(d["choice"] == "review_ungrounded" for d in s.list_decisions())
    assert s.get_pr(pr["pr_id"])["reviewer_approved"] is True


def test_no_num_turns_fast_approval_retried_when_latency_floor_set(
        tmp_errorta_home: Path, tmp_path: Path, monkeypatch) -> None:
    """The other side of the lock: with a non-zero latency floor above the turn's
    wall time, the same sub-floor empty approval IS retried once, then accepted +
    flagged ungrounded (never wedged)."""
    s = _store("gr5", tmp_path)
    ws = _ws("gr5", s)
    n, pr = _run_reviewer_no_num_turns(s, ws, monkeypatch, review_min_latency_ms=3000)
    assert n == 2  # primary + one retry
    assert any(d["choice"] == "review_ungrounded" for d in s.list_decisions())
    assert s.get_pr(pr["pr_id"])["reviewer_approved"] is True
    assert s.get_pr(pr["pr_id"])["review_ungrounded"] is True
