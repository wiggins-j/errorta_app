"""F139 Part B — convergence & integration discipline.

WS-A foundation gate (code-derived clamp + stall), WS-D concurrency ramp,
WS-D2 reactive contract centralization, WS-E convergence stop. All exercised
deterministically without live models.
"""
from pathlib import Path

from errorta_council.coding.autonomy import (
    NOT_CONVERGING,
    CodingAutonomyPolicy,
    LoopCounters,
    _account_convergence,
    _account_foundation_stall,
    effective_parallelism,
    foundation_pending,
    policy_from_dict,
    policy_to_dict,
    runtime_cap,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _CONTRACT_OWNER_TITLE,
    _contract_owner_for,
    foundation_ready,
    refresh_foundation_status,
)
from errorta_council.coding.workspace import CodingWorkspace

_MEMBERS = [("m-pm", "pm"), ("m-dev-1", "dev"), ("m-dev-2", "dev"),
            ("m-rev", "reviewer")]  # 3 non-PM -> static parallelism 3


def _store(pid: str, tmp_path: Path, *, target: str = "new") -> LedgerStore:
    s = LedgerStore(pid, root=tmp_path / f"ledger-{pid}")
    s.create_project(north_star="n", definition_of_done="d", target=target,
                     repo_path=None)
    return s


def _ws(pid: str, store: LedgerStore) -> CodingWorkspace:
    ws = CodingWorkspace(pid, store)
    ws.setup(target="new", repo_path=None)
    return ws


def _merge_file(ws: CodingWorkspace, task_id: str, path: str, content: str) -> None:
    branch = ws.start_task_branch(task_id)
    ws.write_file(path, content, task_id=task_id)
    assert ws.merge_pr(branch).get("merged")


def _record_merged_pr(store: LedgerStore, tid: str) -> None:
    pr = store.record_pr(task_id=tid, branch=f"task-{tid}", head=f"h-{tid}",
                         dev_member="m")
    store.update_pr(pr["pr_id"], status="merged", head=f"h-{tid}")


def _n_foundation_alerts(store: LedgerStore) -> int:
    return sum(1 for d in store.list_decisions()
               if d["choice"] == "foundation_not_converging")


# --- WS-A: foundation_ready + status persistence ----------------------------


def test_foundation_ready_requires_manifest_and_entry(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fr1", tmp_path)
    ws = _ws("fr1", s)
    assert foundation_ready(s, ws) is False           # empty master (.gitignore)
    _merge_file(ws, "t1", "package.json", '{"name": "x"}\n')
    assert foundation_ready(s, ws) is False           # manifest, no source entry
    _merge_file(ws, "t2", "src/index.tsx", "export const App = 1\n")
    assert foundation_ready(s, ws) is True            # manifest + entry


def test_foundation_ready_existing_target_is_always_ready(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fr2", tmp_path, target="existing")
    ws = _ws("fr2", s)  # empty tree, but an existing-target project imports a repo
    assert foundation_ready(s, ws) is True


def test_refresh_foundation_status_persists_and_self_heals(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fr3", tmp_path)
    ws = _ws("fr3", s)
    assert refresh_foundation_status(s, ws) == "pending"
    assert s.get_run_state()["foundation_status"] == "pending"
    assert foundation_pending(s) is True
    _merge_file(ws, "t1", "package.json", '{"name": "x"}\n')
    _merge_file(ws, "t2", "src/app.ts", "export const x = 1\n")
    assert refresh_foundation_status(s, ws) == "merged"
    assert foundation_pending(s) is False


# --- WS-A + WS-D: runtime_cap clamp + ramp ----------------------------------


def test_runtime_cap_is_opt_in_and_clamps_and_ramps(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("rc1", tmp_path)
    policy = CodingAutonomyPolicy()  # AUTO
    base = effective_parallelism(policy, _MEMBERS)
    assert base == 3

    # foundation_status UNSET -> gate not engaged -> full static parallelism.
    assert runtime_cap(policy, _MEMBERS, s) == 3

    # pending -> clamp to 1.
    s.set_run_state(foundation_status="pending")
    assert runtime_cap(policy, _MEMBERS, s) == 1

    # merged but only the foundation merged (<=1 merged PR) -> ramp to min(2, base).
    s.set_run_state(foundation_status="merged")
    _record_merged_pr(s, "foundation")
    assert runtime_cap(policy, _MEMBERS, s) == 2

    # a second (feature) merge -> full parallelism.
    _record_merged_pr(s, "feature")
    assert runtime_cap(policy, _MEMBERS, s) == 3


def test_runtime_cap_pending_clamps_even_explicit_cap(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("rc2", tmp_path)
    s.set_run_state(foundation_status="pending")
    policy = CodingAutonomyPolicy(max_parallel_workers=5)
    assert runtime_cap(policy, _MEMBERS, s) == 1
    # once merged, an explicit cap is honored (no ramp for explicit caps).
    s.set_run_state(foundation_status="merged")
    assert runtime_cap(policy, _MEMBERS, s) == 5


# --- WS-A: foundation stall surfacing ---------------------------------------


def test_foundation_stall_alerts_periodically(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("fs1", tmp_path)
    s.set_run_state(foundation_status="pending")
    c = LoopCounters()
    policy = CodingAutonomyPolicy(foundation_stall_limit=3)
    # Not alerted before the limit; then a heartbeat every `limit` iterations
    # (so a long/resumed run keeps surfacing the stuck foundation).
    for _ in range(3):
        _account_foundation_stall(s, c, policy)
    assert _n_foundation_alerts(s) == 1        # fired at stall == 3
    for _ in range(3):
        _account_foundation_stall(s, c, policy)
    assert _n_foundation_alerts(s) == 2        # re-fired at stall == 6

    # when the foundation merges, the counter resets and stops alerting.
    s.set_run_state(foundation_status="merged")
    _account_foundation_stall(s, c, policy)
    assert c.foundation_stall == 0 and c.foundation_alerted is False
    _account_foundation_stall(s, c, policy)
    assert _n_foundation_alerts(s) == 2        # no new alert once merged


# --- WS-E: convergence stop -------------------------------------------------


def test_convergence_stops_when_nothing_moves(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("cv1", tmp_path)
    c = LoopCounters()
    policy = CodingAutonomyPolicy(convergence_stall_limit=5)
    stop = None
    for i in range(1, 12):
        c.iterations = i
        stop = _account_convergence(s, c, policy)
        if stop is not None:
            break
    assert stop is not None and stop.stop_reason == NOT_CONVERGING
    assert c.iterations - c.last_progress_iter >= 5


def test_convergence_resets_on_any_motion(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("cv2", tmp_path)
    c = LoopCounters()
    policy = CodingAutonomyPolicy(convergence_stall_limit=5)
    # 4 quiet iterations (below the limit)...
    for i in range(1, 5):
        c.iterations = i
        assert _account_convergence(s, c, policy) is None
    # ...then a PR opens (motion) -> fingerprint changes -> streak resets.
    c.iterations = 5
    s.record_pr(task_id="t1", branch="task-t1", head="h", dev_member="m")
    assert _account_convergence(s, c, policy) is None
    assert c.last_progress_iter == 5
    # it now takes another full window of quiet before stopping.
    stop = None
    for i in range(6, 20):
        c.iterations = i
        stop = _account_convergence(s, c, policy)
        if stop is not None:
            break
    assert stop is not None and c.iterations - 5 >= 5


# --- WS-D2: reactive contract centralization --------------------------------


def test_contract_owner_created_deduped_on_mismatch(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    s = _store("co1", tmp_path)
    t = s.add_task(title="feature", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch="task-x", head="h", dev_member="m")

    mismatch = [{"title": "Post field names do not match merged Post type",
                 "body": "", "severity": "blocking"}]
    owner_id = _contract_owner_for(s, pr, mismatch)
    assert owner_id is not None
    assert _CONTRACT_OWNER_TITLE in [t.title for t in s.list_tasks()]

    # a second mismatch reuses the same owner (deduped, not a second task).
    owner_id2 = _contract_owner_for(s, pr, mismatch)
    assert owner_id2 == owner_id
    assert sum(1 for t in s.list_tasks() if t.title == _CONTRACT_OWNER_TITLE) == 1

    # a non-contract finding does not spawn an owner.
    assert _contract_owner_for(s, pr, [{"title": "nit: rename var", "body": ""}]) is None


def test_reviewer_rejection_with_mismatch_makes_revise_depend_on_owner(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """End to end through the real reviewer turn: a contract-mismatch rejection
    spawns the owner task and the revise depends on it."""
    import json

    from errorta_council.coding.runner import build_run_turn, members_by_coding_role
    from errorta_council.coding.topology import DEV, REVIEWER, Assign

    s = _store("co2", tmp_path)
    ws = _ws("co2", s)
    # a dev PR exists to review.
    dev_task = s.add_task(title="build post card", role=DEV)
    ws.start_task_branch(dev_task.task_id)
    ws.write_file("PostCard.tsx", "export const PostCard = 1\n", task_id=dev_task.task_id)
    pr = s.record_pr(task_id=dev_task.task_id, branch=ws.task_branch(dev_task.task_id),
                     head=ws.head(), dev_member="m-dev-1")
    review_task = s.add_task(title=f"review PR: {pr['branch']}", role=REVIEWER,
                             pr_id=pr["pr_id"])

    def caller(_member, _prompt):
        return json.dumps({"schema_version": "coding_turn.v1", "role": "reviewer",
            "task_id": review_task.task_id,
            "intent": {"kind": "review_verdict", "reviewed_head": pr["head"],
                       "approved": False, "findings": [
                {"severity": "blocking",
                 "title": "PostCard import/export mismatch with merged types",
                 "body": "does not match merged Post type", "path": "PostCard.tsx"}]}})

    rt = build_run_turn(s, ws, members_by_coding_role([
        {"id": "m-rev", "enabled": True, "metadata": {"coding_role": "reviewer"}}]),
        caller, guardrail_enabled=True)
    rt(Assign(member_id="m-rev", task_id=review_task.task_id, role=REVIEWER), s)

    tasks = {t.title: t for t in s.list_tasks()}
    assert _CONTRACT_OWNER_TITLE in tasks
    owner = tasks[_CONTRACT_OWNER_TITLE]
    revise = next(t for t in s.list_tasks() if t.title.startswith("revise:"))
    assert owner.task_id in (revise.depends_on or [])


# --- policy round-trip ------------------------------------------------------


def test_policy_roundtrip_includes_part_b_fields() -> None:
    p = CodingAutonomyPolicy(foundation_stall_limit=7, convergence_stall_limit=9)
    p2 = policy_from_dict(policy_to_dict(p))
    assert p2.foundation_stall_limit == 7
    assert p2.convergence_stall_limit == 9


# --- review fixes -----------------------------------------------------------


def test_existing_target_is_never_gated(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 review F1: an imported (`existing`) repo already has a foundation — the
    gate stays disengaged, so full parallelism (no clamp, no ramp) from iteration 0."""
    from errorta_council.coding.runner import refresh_foundation_status

    s = _store("ex1", tmp_path, target="existing")
    ws = _ws("ex1", s)
    assert refresh_foundation_status(s, ws) == "n/a"
    assert "foundation_status" not in s.get_run_state()
    assert foundation_pending(s) is False
    assert runtime_cap(CodingAutonomyPolicy(), _MEMBERS, s) == 3  # full, no ramp


def test_convergence_resets_on_productive_pm_planning(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 review F5: a PM that keeps (re)planning — changing the task set — is
    motion, so the convergence stop must not fire on productive planning."""
    s = _store("cv3", tmp_path)
    c = LoopCounters()
    policy = CodingAutonomyPolicy(convergence_stall_limit=4)
    for i in range(1, 10):
        c.iterations = i
        if i % 2 == 0:
            s.add_task(title=f"plan step {i}", role="dev")  # PM planning = motion
        assert _account_convergence(s, c, policy) is None, f"false stop at iter {i}"


def test_sequential_loop_upgrades_to_concurrent_when_clamp_lifts(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 review F2: once the foundation merges mid-run, a clamped (sequential)
    run must hand back UP to the concurrent loop even with checkpoint cadence off —
    otherwise fan-out never resumes. We assert the hand-off decision directly:
    runtime_cap lifts above 1 as soon as foundation_status flips to merged."""
    s = _store("up1", tmp_path)
    policy = CodingAutonomyPolicy()
    s.set_run_state(foundation_status="pending")
    assert runtime_cap(policy, _MEMBERS, s) == 1     # clamped -> sequential loop
    # foundation lands:
    s.set_run_state(foundation_status="merged")
    _record_merged_pr(s, "foundation")
    assert runtime_cap(policy, _MEMBERS, s) == 2      # clamp lifts -> loop upgrades


def test_contract_owner_deduped_across_owner_pr_open(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 review F4: the owner task flips to `done` when it opens a PR; a later
    mismatch must still reuse it (run_state-id dedup), not spawn a duplicate."""
    s = _store("co3", tmp_path)
    t = s.add_task(title="feature", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch="task-x", head="h", dev_member="m")
    mismatch = [{"title": "does not match merged Post type", "body": "",
                 "severity": "blocking"}]
    owner_id = _contract_owner_for(s, pr, mismatch)
    assert owner_id is not None
    # simulate the owner opening a PR -> its task becomes `done`.
    s.update_task(owner_id, state="done")
    owner_id2 = _contract_owner_for(s, pr, mismatch)
    assert owner_id2 == owner_id
    assert sum(1 for t in s.list_tasks() if t.title == _CONTRACT_OWNER_TITLE) == 1


def test_contract_classifier_ignores_local_findings(
        tmp_errorta_home: Path, tmp_path: Path) -> None:
    """F139 review F3: a mismatch phrase without a shared-contract noun is a local
    bug, not a cross-cutting contract — do not spawn an owner."""
    s = _store("co4", tmp_path)
    t = s.add_task(title="feature", role="dev")
    pr = s.record_pr(task_id=t.task_id, branch="task-x", head="h", dev_member="m")
    # "does not match" but no contract noun -> local assertion bug.
    assert _contract_owner_for(
        s, pr, [{"title": "assertion does not match expected output", "body": ""}]) is None
    # with a contract noun it does fire.
    assert _contract_owner_for(
        s, pr, [{"title": "return type does not match", "body": ""}]) is not None
