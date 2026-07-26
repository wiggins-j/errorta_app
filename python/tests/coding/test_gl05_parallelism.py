"""GL05 — single-vs-multi & parallelism.

Two halves, flagged NET-NEW vs ASSERTED-EXISTING:

* Item 1 (NET-NEW) — the role-justification topology audit: a role that adds no
  distinct signal per the SPEC-15 manifest (executor-less TESTER, ungrounded
  REVIEWER) is flagged; GL01/GL02-style distinct-signal roles and the PM+DEV
  baseline pass.
* Item 2 (NET-NEW) — the strict a-priori file-ownership partition: two in-flight
  tasks may never own the same declared file, from tick 0 (before any conflict);
  disjoint-file and prose-silent tasks are unaffected.
* Item 3 (ASSERTED-EXISTING) — serialized integration: a mergeable PR is an
  exclusive Merge, never dispatched alongside a worker Assign.
* Item 4 (ASSERTED-EXISTING) — the parallelism-health brake IS GL04's
  convergence clamp: `runtime_cap` honors the clamp flag -> serial.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    inflight_owned_paths,
    policy_from_dict,
    policy_to_dict,
    runtime_cap,
)
from errorta_council.coding.capabilities import RoleCapability
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.topology import (
    DEV,
    PM,
    REVIEWER,
    TESTER,
    Assign,
    Merge,
    plan_next_batch,
)
from errorta_council.coding.topology_audit import (
    audit_topology,
    topology_advisories,
)

TEAM = [("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV), ("m-rev", REVIEWER)]


def _store(tmp_path: Path, name: str = "gl05") -> LedgerStore:
    s = LedgerStore(name, root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _assigns(batch) -> list[Assign]:
    return [a for a in batch if isinstance(a, Assign)]


def _cap(role, *, repo_read=False, can_execute=False, gate_available=False,
         tools=("code_write",), can_dispatch=None) -> RoleCapability:
    # SPEC-26: the TESTER's justifying signal is now `can_dispatch` (is there a
    # UNIT-scoped command a tester turn could actually run?), not `gate_available`
    # (does the ENGINE have an acceptance gate?). `can_dispatch` defaults to
    # `gate_available` here so every pre-existing case in this file keeps meaning
    # what it meant; the cases that pull them apart pass it explicitly.
    return RoleCapability(
        role=role, tools=tuple(tools), repo_read=repo_read,
        can_execute=can_execute, gate_available=gate_available, summary="",
        can_dispatch=gate_available if can_dispatch is None else can_dispatch)


def _manifest(overrides: dict | None = None) -> dict[str, RoleCapability]:
    # NB: keys are the role *constants* (values "pm"/"dev"/…), so overrides must be a
    # dict — a `REVIEWER=` kwarg would key on the literal name, not the constant.
    base = {
        PM: _cap(PM, tools=()),
        DEV: _cap(DEV, tools=("code_write",)),
        TESTER: _cap(TESTER, gate_available=True),        # grounded by default
        REVIEWER: _cap(REVIEWER, repo_read=True),         # grounded by default
    }
    if overrides:
        base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Item 1 (NET-NEW) — role-justification topology audit
# --------------------------------------------------------------------------- #

def test_ungrounded_reviewer_is_flagged():
    # A REVIEWER with no repo-read and no execute is "another ungrounded opinion in
    # the same loop" — a redundant seat that adds no distinct signal.
    man = _manifest({REVIEWER: _cap(REVIEWER, repo_read=False, can_execute=False)})
    adv = topology_advisories(man)
    assert len(adv) == 1
    assert "REVIEWER" in adv[0]
    assert "grant" in adv[0].lower() or "delete" in adv[0].lower()


def test_executorless_tester_is_flagged():
    # A TESTER whose duty is execution but whose manifest grants no gate/executor is
    # a verification role that structurally never fires (MAST FM-3.2).
    man = _manifest({TESTER: _cap(TESTER, gate_available=False, can_execute=False)})
    adv = topology_advisories(man)
    assert len(adv) == 1
    assert "TESTER" in adv[0]


def test_distinct_signal_roles_pass():
    # GL01 grounds the TESTER (gate), GL02 grounds the REVIEWER (repo-read): both now
    # supply a distinct signal, so neither is flagged.
    verdicts = {j.role: j for j in audit_topology(_manifest())}
    assert verdicts[TESTER].justified
    assert verdicts[TESTER].signal == "execution"
    assert verdicts[REVIEWER].justified
    assert verdicts[REVIEWER].signal == "grounded review"
    assert topology_advisories(_manifest()) == []


def test_all_passing_roles_produce_no_flag():
    # A fully grounded four-role council seats all four with no advisory.
    assert topology_advisories(_manifest()) == []


def test_pm_dev_baseline_always_passes():
    # PM+DEV alone (single-agent-plus-coordination) is the baseline the whole RQ5
    # principle defends — never flagged, even with no TESTER/REVIEWER seated.
    verdicts = audit_topology(_manifest(), seated_roles=(PM, DEV))
    assert all(j.justified for j in verdicts)
    assert {j.role for j in verdicts} == {PM, DEV}
    assert topology_advisories(_manifest(), seated_roles=(PM, DEV)) == []


def test_seated_roles_scope_the_audit():
    # An ungrounded REVIEWER that is NOT seated is not audited (the DELETE half).
    man = _manifest({REVIEWER: _cap(REVIEWER, repo_read=False)})
    assert topology_advisories(man, seated_roles=(PM, DEV, TESTER)) == []
    assert topology_advisories(man, seated_roles=(PM, DEV, REVIEWER)) != []


# --------------------------------------------------------------------------- #
# Item 2 (NET-NEW) — strict a-priori file-ownership partition
# --------------------------------------------------------------------------- #

def test_inflight_owned_paths_holds_declared_and_observed(tmp_path: Path):
    s = _store(tmp_path)
    # A `doing` task with a DECLARED path.
    doing = s.add_task(title="doing", role=DEV, target_files=["src/store.ts"])
    s.update_task(doing.task_id, state="doing")
    # A prose-silent task whose OPEN PR OBSERVED a changed path.
    silent = s.add_task(title="Add activity feed", role=DEV)
    s.update_task(silent.task_id, state="doing")
    pr = s.record_pr(task_id=silent.task_id, branch="br", head="h", dev_member="m-dev1")
    s.update_pr(pr["pr_id"], changed_paths=["src/feed.ts"])
    owned = inflight_owned_paths(s)
    assert "src/store.ts" in owned    # declared
    assert "src/feed.ts" in owned     # observed


def test_a_priori_partition_holds_second_toucher_before_any_conflict(tmp_path: Path):
    # THE LINCHPIN: two tasks contend for one file with ZERO conflict history (so the
    # reactive F159 hot-file gate is inert). The a-priori partition still holds the
    # second toucher until the first's PR merges.
    s = _store(tmp_path)
    owner = s.add_task(title="owner", role=DEV, target_files=["src/store.ts"])
    s.update_task(owner.task_id, state="doing")
    second = s.add_task(title="second", role=DEV, target_files=["src/store.ts"])
    other = s.add_task(title="other", role=DEV, target_files=["src/other.ts"])

    owned = inflight_owned_paths(s)
    assert owned == {"src/store.ts"}   # no conflict needed — held from tick 0

    idle = [("m-dev2", DEV)]           # dev1 busy on the owner
    ids = {a.task_id for a in _assigns(plan_next_batch(s, idle, owned_paths=owned))}
    assert second.task_id not in ids   # held — waits for owner's PR to merge
    assert other.task_id in ids        # disjoint file → dispatched in parallel


def test_partition_lifts_when_owner_merges(tmp_path: Path):
    # Once the owner's PR merges, the file is free and the second toucher dispatches.
    s = _store(tmp_path)
    owner = s.add_task(title="owner", role=DEV, target_files=["src/store.ts"])
    s.update_task(owner.task_id, state="doing")
    pr = s.record_pr(task_id=owner.task_id, branch="br", head="h", dev_member="m-dev1")
    second = s.add_task(title="second", role=DEV, target_files=["src/store.ts"])
    assert "src/store.ts" in inflight_owned_paths(s)

    s.update_pr(pr["pr_id"], status="merged")
    s.update_task(owner.task_id, state="done")
    assert inflight_owned_paths(s) == set()  # nothing in flight owns it now
    ids = {a.task_id for a in _assigns(plan_next_batch(
        s, [("m-dev2", DEV)], owned_paths=inflight_owned_paths(s)))}
    assert second.task_id in ids


def test_two_ready_same_file_tasks_dispatch_one_at_a_time(tmp_path: Path):
    # Within a SINGLE batch (both idle, nothing in flight yet): two ready tasks that
    # both declare the same file → only ONE is dispatched this tick (the within-batch
    # `owned |= tp` claim), never two in-flight owners of that file.
    s = _store(tmp_path)
    a = s.add_task(title="A", role=DEV, target_files=["src/store.ts"])
    b = s.add_task(title="B", role=DEV, target_files=["src/store.ts"])
    ids = {x.task_id for x in _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)], owned_paths=set()))}
    assert len(ids & {a.task_id, b.task_id}) == 1


def test_disjoint_file_tasks_run_in_parallel(tmp_path: Path):
    # Distinct declared files → both dispatch concurrently (no over-serialization).
    s = _store(tmp_path)
    a = s.add_task(title="A", role=DEV, target_files=["src/a.ts"])
    b = s.add_task(title="B", role=DEV, target_files=["src/b.ts"])
    ids = {x.task_id for x in _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)], owned_paths=set()))}
    assert ids == {a.task_id, b.task_id}


def test_prose_silent_tasks_are_not_over_serialized(tmp_path: Path):
    # Fail-open on silence: a task declaring NO paths is unknown ownership, not
    # universal — two prose-silent tasks both dispatch even under the partition.
    s = _store(tmp_path)
    a = s.add_task(title="Add real-time indicators", role=DEV)
    b = s.add_task(title="Add a settings page", role=DEV)
    owner = s.add_task(title="owner", role=DEV, target_files=["src/store.ts"])
    s.update_task(owner.task_id, state="doing")
    owned = inflight_owned_paths(s)  # {src/store.ts}
    ids = {x.task_id for x in _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)], owned_paths=owned))}
    assert {a.task_id, b.task_id} <= ids  # neither prose-silent task is held


def test_owned_paths_none_is_identical_to_pre_gl05(tmp_path: Path):
    # `owned_paths=None` (or empty) restores byte-identical pre-GL05 dispatch.
    s = _store(tmp_path)
    a = s.add_task(title="A", role=DEV, target_files=["src/store.ts"])
    b = s.add_task(title="B", role=DEV, target_files=["src/store.ts"])
    ids = {x.task_id for x in _assigns(plan_next_batch(
        s, [("m-dev1", DEV), ("m-dev2", DEV)]))}
    # With no partition and no hot history, both dispatch (as pre-GL05).
    assert ids == {a.task_id, b.task_id}


# --------------------------------------------------------------------------- #
# Item 3 (ASSERTED-EXISTING) — serialized integration invariant
# --------------------------------------------------------------------------- #

def test_mergeable_pr_is_exclusive_never_mixed_with_assign(tmp_path: Path):
    # Serial integration: a mergeable PR yields ONLY a Merge — never an Assign
    # dispatched alongside it — so a merge never mutates master while a worker builds
    # on the base it is changing. This locks the planner half of the invariant.
    s = _store(tmp_path)
    ready = s.add_task(title="ready dev work", role=DEV, target_files=["src/x.ts"])
    owner = s.add_task(title="pr owner", role=DEV)
    pr = s.record_pr(task_id=owner.task_id, branch="br", head="h", dev_member="m-dev1")
    s.update_pr(pr["pr_id"], status="mergeable")
    batch = plan_next_batch(s, TEAM)
    assert len(batch) == 1
    assert isinstance(batch[0], Merge)
    assert not _assigns(batch)          # no worker Assign rides alongside the Merge
    assert ready.task_id  # (the ready task waits until integration drains)


# --------------------------------------------------------------------------- #
# Item 4 (ASSERTED-EXISTING) — parallelism-health brake = GL04 clamp
# --------------------------------------------------------------------------- #

def test_convergence_clamp_is_the_rq6_health_brake(tmp_path: Path):
    # GL05 Item 4 asserts GL04's superseded-ratio clamp IS the "is parallelism paying
    # off?" brake: when the clamp flag is set, runtime_cap forces serial (1), freezing
    # fan-out — no second health signal is added.
    s = _store(tmp_path)
    members = [("m-pm", PM), ("m-dev1", DEV), ("m-dev2", DEV)]
    policy = CodingAutonomyPolicy(max_parallel_workers=2)
    assert runtime_cap(policy, members, s) == 2     # unclamped → fan out
    s.set_run_state(convergence_clamped=True)
    assert runtime_cap(policy, members, s) == 1     # clamped → serial integration
    s.set_run_state(convergence_clamped=False)
    assert runtime_cap(policy, members, s) == 2     # released → fan out restored


# --------------------------------------------------------------------------- #
# Policy plumbing — the new strict_file_partition flag round-trips.
# --------------------------------------------------------------------------- #

def test_strict_file_partition_defaults_on_and_round_trips():
    assert CodingAutonomyPolicy().strict_file_partition is True
    d = policy_to_dict(CodingAutonomyPolicy())
    assert d["strict_file_partition"] is True
    assert policy_from_dict({"strict_file_partition": False}).strict_file_partition is False
