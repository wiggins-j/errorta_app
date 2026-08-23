# python/tests/liverun/test_fixloop.py
"""The fix-cycle driver, against fakes for every engine seam (spec §3.5).

The four paths with no prior coverage anywhere (spec §4) are each pinned here
before anything else: the accept staging, the cancel path, the guarded-path
predicate, and a deploy step that fails.
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

import errorta_liverun
from errorta_liverun import fixloop as F
from errorta_liverun import profile as P
from errorta_liverun.brief import EvidenceBundle, EvidenceItem
from errorta_liverun.fixloop import (
    GUARDED_PATH_PREFIXES,
    FixCycle,
    FixDeps,
    escapes_repo,
    is_human_only_diff,
)
from errorta_liverun.steps import StepResult


@pytest.fixture(autouse=True)
def _home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeStore:
    """Stand-in for `errorta_council.coding.ledger.LedgerStore`."""

    def __init__(self) -> None:
        self.tasks: list[dict] = []
        self.state: dict = {"status": "idle"}
        self.log: list[dict] = []
        self.commands = {"pytest-unit": {"argv": ["python3", "-m", "pytest", "-q"],
                                         "label": "unit"}}
        # Only an `existing`-target project has a repository to merge back
        # into; the cycle refuses the rest before it files a task.
        self.target = "existing"

    def get_project(self):
        return SimpleNamespace(target=self.target, repo_path="/r/senditai-ng",
                               delivery_root=None)

    def add_task(self, **kw):
        self.tasks.append(kw)
        return SimpleNamespace(task_id=f"t{len(self.tasks)}")

    def add_focus(self, **kw):
        self.focuses = getattr(self, "focuses", [])
        self.focuses.append(kw)
        return SimpleNamespace(id=f"f{len(self.focuses)}")

    def get_run_state(self) -> dict:
        return dict(self.state)

    def set_run_state(self, **patch) -> dict:
        self.state.update(patch)
        return dict(self.state)

    def get_test_commands(self) -> dict:
        return dict(self.commands)


class FakeWs:
    def __init__(self) -> None:
        self.head_value = "h0"
        self.changed = ["senditai_ng/agent/plan.py"]
        self.calls: list[tuple[str, str]] = []

    def head(self) -> str:
        return self.head_value

    def changed_paths(self, branch: str, *, base: str = "master") -> list[str]:
        self.calls.append((branch, base))
        return list(self.changed)


class Fake:
    """Every FixDeps seam, plus the knobs a test turns."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.store = FakeStore()
        self.ws = FakeWs()
        self.gate = True
        self.merge_gate = True
        self.started: list[tuple[str, dict]] = []
        self.staged: list[tuple[str, dict, str, str]] = []
        self.confirmations: dict[str, dict] = {}
        self.confirm_state = "approved"
        self.resolved: list[tuple[str, str]] = []
        self.accept_outcome: dict | None = None
        self.action_results: dict[str, StepResult] = {}
        self.actions: list[str] = []
        self.checks: list[str] = []
        self.check_starts: list[float] = []
        self.check_ok = True
        self.banned: list[str] = []
        self.triage_replies: list[str] = []
        self.start_raises: Exception | None = None
        self.run_end_status = "stopped"
        self.start_result: dict = {"status": "started"}
        self.start_sets_running = True
        self.dev_routes: list[str] = ["claude_cli.sonnet"]
        self.assigned: list[tuple[str, str]] = []
        self.assign_raises: Exception | None = None
        self.seed_result = False
        self.seeded: list[str] = []
        self.seed_raises: Exception | None = None
        self.ws_raises: Exception | None = None

    # -- seams ---------------------------------------------------------- #
    def deps(self) -> FixDeps:
        return FixDeps(
            ledger_factory=lambda pid: self.store,
            workspace_factory=self._workspace,
            gate_available_fn=lambda store: self.gate,
            merge_gate_ok_fn=lambda store, ws: self.merge_gate,
            start_run_fn=self._start_run,
            team_log_fn=lambda store: list(self.store.log),
            stage_confirmation_fn=self._stage,
            get_confirmation_fn=lambda cid: dict(self.confirmations[cid])
            if cid in self.confirmations else None,
            resolve_confirmation_fn=self._resolve,
            accept_outcome_fn=(lambda cid: self.accept_outcome)
            if self.accept_outcome is not None else None,
            triage_fn=(lambda prompt, project_id, route: self.triage_replies.pop(0))
            if self.triage_replies else None,
            bound_channel_fn=lambda pid: "C-live",
            assign_dev_route_fn=self._assign_dev_route,
            seed_workspace_fn=self._seed,
        )

    def _assign_dev_route(self, project_id: str, route: str) -> list[str]:
        if self.assign_raises is not None:
            raise self.assign_raises
        prior = [r for r in self.dev_routes if r != route]
        self.assigned.append((project_id, route))
        self.dev_routes = [route for _ in self.dev_routes]
        return prior

    def _seed(self, project_id: str) -> bool:
        if self.seed_raises is not None:
            raise self.seed_raises
        self.seeded.append(project_id)
        return self.seed_result

    def _workspace(self, project_id: str):
        if self.ws_raises is not None:
            raise self.ws_raises
        return self.ws

    def finish_run(self, status: str | None = None) -> None:
        """The dev run ends. A run that delivered anything moved the branch —
        `head` is how the cycle later tells delivered work from none."""
        self.store.state["status"] = status or self.run_end_status
        if self.ws.changed:
            self.ws.head_value = "h1"

    def _start_run(self, project_id: str, *, resume: bool, continue_: bool) -> dict:
        if self.start_raises is not None:
            raise self.start_raises
        self.started.append((project_id, {"resume": resume, "continue_": continue_}))
        if self.start_sets_running and self.start_result.get("status") == "started":
            self.store.state["status"] = "running"
        return dict(self.start_result)

    def _resolve(self, cid: str, decision: str):
        """The store's atomic claim, faithfully: only a PENDING record moves,
        and the caller is told whether IT was the one that moved it."""
        record = self.confirmations[cid]                      # KeyError if unknown
        self.resolved.append((cid, decision))
        if record.get("state") != "pending":
            return dict(record), False
        record["state"] = decision
        return dict(record), True

    def _stage(self, verb: str, args: dict, thread_ts: str, *, channel_id: str = "") -> str:
        cid = f"cid{len(self.staged)}"
        self.staged.append((verb, dict(args), thread_ts, channel_id))
        self.confirmations[cid] = {"id": cid, "verb": verb, "args": dict(args),
                                   "state": self.confirm_state}
        return cid

    def run_action(self, action, ctx, *, timeout_s):
        name = action.params.get("argv", ("?",))[0]
        self.actions.append(name)
        res = self.action_results.get(name)
        return res if res is not None else StepResult(True, "t", "t", exit_code=0)

    def run_check(self, check, ctx, *, step_start):
        self.checks.append(check.kind)
        self.check_starts.append(step_start)
        return self.check_ok

    def ban_scan(self, text: str, *, where: str) -> bool:
        if "Account is banned" in (text or ""):
            self.banned.append(where)
            return True
        return False


def _deploy_steps() -> tuple[P.Step, ...]:
    act = P.Action("local", {"argv": ("/usr/bin/rsync", "-az", "/src/", "h:dst/"), "cwd": None})
    return (P.Step("rsync", act, None, 300),)


def _profile(*, fixable: bool = True, deploy: bool = True) -> P.Profile:
    return P.Profile(
        name="osrs", hosts={}, tunnels={}, launch=(), watch=(), evidence=(), teardown=(),
        caps=P.DEFAULT_CAPS, ban_signals=("Account is banned",),
        repos=(
            P.RepoDef("brain", "/r/senditai-ng", "senditai-ng", fixable,
                      ("python_traceback", "brain_log_stall", "journal_stall",
                       "brain_pid_dead"), _deploy_steps() if deploy else ()),
            P.RepoDef("reaper", "/r/osrs-reaper", "osrs-reaper", False,
                      ("jvm_exception", "client_port_dead", "client_state_stale"), ()),
        ),
        fix_loop=P.FixLoop(enabled=True))


def _bundle(**over) -> EvidenceBundle:
    kw = dict(run_id="lr-1", profile_name="osrs", stop_reason="stall:brain-log",
              stalled_probe_id="brain-log", stalled_s=187.0, launch_step_name=None,
              literals={"logoff_verified": True},
              evidence=(EvidenceItem("brain-log-tail", False, "quiet", "nothing here", "",
                                     ("/ev/brain-log-tail.stdout",)),),
              evidence_dir="/ev")
    kw.update(over)
    return EvidenceBundle(**kw)


def _cycle(fake: Fake, *, prof: P.Profile | None = None, idle_timeout_s: float = 1200,
           accept_timeout_s: float = 1800, bundle: EvidenceBundle | None = None) -> FixCycle:
    return FixCycle(bundle or _bundle(), prof or _profile(), None, fake.deps(),
                    run_id="lr-1", project_id="live-proj", clock=fake.clock,
                    wall=fake.clock, idle_timeout_s=idle_timeout_s,
                    accept_timeout_s=accept_timeout_s, ctx=None,
                    run_action=fake.run_action, run_check=fake.run_check,
                    ban_scan=fake.ban_scan)


def _drive(cyc: FixCycle, fake: Fake, *, limit: int = 40):
    """Tick until the cycle stops being `pending`, advancing the clock past the
    watch poll each time. The dev run finishes on its first watched tick — the
    idle detector has its own tests. Returns the last outcome, carrying every
    event the drive produced."""
    events: list[tuple[str, dict]] = []
    out = None
    for _ in range(limit):
        out = cyc.step()
        events.extend(out.events)
        if out.kind in ("paused", "deployed"):
            break
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run()
        fake.clock.advance(31)
    assert out is not None
    out.events[:] = events
    return out


# -- the happy path -------------------------------------------------------- #

def test_happy_path_files_a_task_starts_a_run_and_deploys() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    out = _drive(cyc, fake)
    assert out.kind == "deployed" and out.failed is False
    task = fake.store.tasks[0]
    assert task["role"] == "dev" and task["task_type"] == "implementation"
    assert "UNTRUSTED LIVE-RUN EVIDENCE" in task["detail"]
    assert task["title"].startswith("Fix: ")
    assert fake.started == [("senditai-ng", {"resume": False, "continue_": False})]
    assert [k for k, _ in out.events] == [
        "fix_triage", "fix_team_model", "fix_task", "fix_run", "fix_accept_staged",
        "fix_accepted", "deploy_step"]
    assert cyc.repo_id == "brain" and cyc.task_id == "t1"


def test_the_run_goes_terminal_before_the_diff_is_ever_read() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()          # triage, task, run
    assert fake.ws.calls == []                  # still running: nothing read
    fake.finish_run("stopped")
    fake.clock.advance(31)
    cyc.step()                                  # watch sees terminal
    fake.clock.advance(31)
    cyc.step()                                  # stage reads the diff
    assert fake.ws.calls == [("master", "h0")]


@pytest.mark.parametrize("status,mode", [
    ("idle", {"resume": False, "continue_": False}),
    ("interrupted", {"resume": True, "continue_": False}),
    ("stopped", {"resume": False, "continue_": True}),
])
def test_run_mode_is_read_off_the_projects_run_state(status, mode) -> None:
    fake = Fake()
    fake.store.state = {"status": status}
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    assert fake.started == [("senditai-ng", mode)]


def test_the_fix_task_event_names_task_repo_and_project() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step()
    detail = cyc.step().events[0][1]
    assert detail["task_id"] == "t1" and detail["repo_id"] == "brain"
    assert detail["project_id"] == "senditai-ng" and detail["gate"]


def test_dev_seat_is_moved_to_the_profile_route_before_the_task_is_filed() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert fake.assigned == [("senditai-ng", "claude_cli.opus")]
    kinds = [k for k, _ in out.events]
    assert kinds.index("fix_team_model") < kinds.index("fix_task")
    ev = dict(out.events)["fix_team_model"]
    assert ev == {"project_id": "senditai-ng", "role": "dev",
                  "from": ["claude_cli.sonnet"], "to": "claude_cli.opus"}


def test_dev_seat_already_on_the_route_is_left_alone() -> None:
    fake = Fake()
    fake.dev_routes = ["claude_cli.opus", "claude_cli.opus"]
    out = _drive(_cycle(fake), fake)
    assert fake.assigned == [("senditai-ng", "claude_cli.opus")]  # asked once
    assert "fix_team_model" not in [k for k, _ in out.events]


def test_unavailable_dev_route_pauses_before_any_run() -> None:
    fake = Fake()
    fake.assign_raises = RuntimeError("model_not_found")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail.startswith("dev_route_unavailable:claude_cli.opus")
    assert fake.started == [] and fake.store.tasks == []


def test_missing_worktree_is_seeded_before_the_workspace_opens() -> None:
    fake = Fake()
    fake.seed_result = True
    out = _drive(_cycle(fake), fake)
    assert fake.seeded == ["senditai-ng"]
    assert dict(out.events)["fix_workspace_seeded"] == {
        "project_id": "senditai-ng", "repo_path": "/r/senditai-ng"}


def test_existing_worktree_is_not_reseeded_and_not_announced() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert fake.seeded == ["senditai-ng"]
    assert "fix_workspace_seeded" not in [k for k, _ in out.events]


def test_seed_failure_pauses_with_the_exception_named() -> None:
    fake = Fake()
    fake.seed_raises = ValueError("existing target needs a valid repo_path")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail == "seed:ValueError"
    assert fake.started == []


def test_workspace_failure_keeps_its_message() -> None:
    fake = Fake()
    fake.ws_raises = RuntimeError("no worktree for this project yet")
    out = _drive(_cycle(fake), fake)
    assert out.kind == "paused" and out.code == "fix_run_failed"
    assert out.detail == "RuntimeError:no worktree for this project yet"


# -- the cancel path ------------------------------------------------------- #

def test_idle_run_is_cancelled_through_cancel_requested() -> None:
    fake = Fake()
    cyc = _cycle(fake, idle_timeout_s=1200)
    cyc.step(); cyc.step(); cyc.step()          # into the watch
    fake.clock.advance(1201)
    out = cyc.step()
    assert fake.store.state["cancel_requested"] is True
    assert [k for k, _ in out.events] == ["fix_idle_cancel"]
    assert out.kind == "pending"
    fake.clock.advance(121)                     # never goes terminal
    final = cyc.step()
    assert final.kind == "paused" and final.code == "fix_idle" and final.failed is True


def test_a_cancelled_run_that_does_go_terminal_is_still_a_failed_cycle() -> None:
    fake = Fake()
    cyc = _cycle(fake, idle_timeout_s=1200)
    cyc.step(); cyc.step(); cyc.step()
    fake.clock.advance(1201)
    cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31)
    out = cyc.step()
    assert out.code == "fix_idle" and out.failed is True


def test_progress_in_the_team_log_resets_the_idle_clock() -> None:
    fake = Fake()
    cyc = _cycle(fake, idle_timeout_s=1200)
    cyc.step(); cyc.step(); cyc.step()
    fake.clock.advance(1100); cyc.step()
    fake.store.log.append({"at": "2026-08-22T03:00:00Z", "kind": "x", "message": "y"})
    fake.clock.advance(200); cyc.step()
    assert fake.store.state.get("cancel_requested") is not True


def test_the_idle_clock_is_wall_time_not_a_tick_count() -> None:
    fake = Fake()
    cyc = _cycle(fake, idle_timeout_s=1200)
    cyc.step(); cyc.step(); cyc.step()
    for _ in range(60):                         # 60 ticks, 30s apart = 1800s
        fake.clock.advance(10)                  # ... but only 600s of wall time
        cyc.step()
    assert fake.store.state.get("cancel_requested") is not True


# -- entry guards ---------------------------------------------------------- #

def test_no_gate_pauses_before_any_task_is_filed() -> None:
    fake = Fake()
    fake.gate = False
    out = _cycle(fake).step()
    assert out.kind == "paused" and out.code == "fix_no_gate"
    assert fake.store.tasks == [] and fake.started == []


def test_default_assign_dev_route_over_a_real_ledger_store(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`_default_assign_dev_route` is the seam's only production implementation
    and, before this test, had no coverage of its own -- everything else drives
    it through the `Fake`. Exercise it directly against a real `LedgerStore`
    (the fixture-provided `ERRORTA_HOME` already points at `tmp_path`).

    Covers: (a) a single-mode dev on another route is reseated; (b) every dev
    already single-mode on the route is a no-op; (c) a MULTI-mode dev whose
    `gateway_route_id` already equals the route is still reseated (to single);
    (d) a non-dev member is neither counted nor touched.
    """
    from errorta_council.coding import control_actions, pm_reference
    from errorta_council.coding.ledger import LedgerStore

    monkeypatch.setattr(
        pm_reference, "list_available_routes",
        lambda: [{"route_id": "claude_cli.opus", "family": "opus",
                  "provider_class": "claude_cli"}])
    calls: list[tuple] = []
    real_assign = control_actions.assign_models_by_role

    def _spy(store, role_routes, **kw):
        calls.append((role_routes, kw))
        return real_assign(store, role_routes, **kw)

    monkeypatch.setattr(control_actions, "assign_models_by_role", _spy)

    # (a) single-mode dev on another route -> action called, prior route
    # returned, run_config now shows opus.
    store_a = LedgerStore("unit-a")
    store_a.set_run_config(members=[
        {"id": "d1", "coding_role": "dev", "gateway_route_id": "claude_cli.sonnet",
         "model_mode": "single"}])
    prior_a = F._default_assign_dev_route("unit-a", "claude_cli.opus")
    assert prior_a == ["claude_cli.sonnet"]
    assert len(calls) == 1
    member_a = store_a.get_run_config()["members"][0]
    assert member_a["gateway_route_id"] == "claude_cli.opus"
    assert member_a["model_mode"] == "single"

    # (b) every dev already single-mode on the route -> no call, [].
    prior_b = F._default_assign_dev_route("unit-a", "claude_cli.opus")
    assert prior_b == [] and len(calls) == 1

    # (c) multi-mode dev already on the route -> still reseated (mode -> single).
    store_c = LedgerStore("unit-c")
    store_c.set_run_config(members=[
        {"id": "d1", "coding_role": "dev", "gateway_route_id": "claude_cli.opus",
         "model_mode": "multi", "model_pool": ["claude_cli.opus", "claude_cli.sonnet"]}])
    prior_c = F._default_assign_dev_route("unit-c", "claude_cli.opus")
    assert prior_c == ["claude_cli.opus"]
    assert len(calls) == 2
    member_c = store_c.get_run_config()["members"][0]
    assert member_c["model_mode"] == "single"
    assert member_c["gateway_route_id"] == "claude_cli.opus"
    assert "model_pool" not in member_c

    # (d) a non-dev member on another route is not counted or changed.
    store_d = LedgerStore("unit-d")
    store_d.set_run_config(members=[
        {"id": "r1", "coding_role": "reviewer", "gateway_route_id": "claude_cli.sonnet",
         "model_mode": "single"}])
    prior_d = F._default_assign_dev_route("unit-d", "claude_cli.opus")
    assert prior_d == [] and len(calls) == 2
    member_d = store_d.get_run_config()["members"][0]
    assert member_d["gateway_route_id"] == "claude_cli.sonnet"


def test_default_assign_dev_route_probes_a_cold_cli_route_before_seating(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 20260823T060652-05f8b9: a freshly restarted sidecar has never
    warmed the gateway's in-memory `_PROBE_CACHE` -- only the desktop panel's
    Test button does that -- so `resolve_route_availability` reports a live
    `claude_cli.*` route unavailable/`cli_not_verified` even when the CLI
    itself is logged in, and `assign_models_by_role` raised
    `ControlActionError("model_not_found", "no available model matches ...")`,
    pausing the fix cycle. `_default_assign_dev_route` must now run the SAME
    probe the Test button runs, synchronously, before seating: warm the
    cache, then let `assign_models_by_role` resolve normally.
    """
    from errorta_app.routes import gateway as gw
    from errorta_council.coding import model_availability, pm_reference
    from errorta_council.coding.ledger import LedgerStore

    probe_calls: list[str] = []

    async def _fake_probe(provider: str) -> dict:
        probe_calls.append(provider)
        return {"ok": True, "detail": "ok", "latency_ms": 1,
                "state": "connected", "remediation": ""}

    monkeypatch.setattr(gw, "probe_cli_provider", _fake_probe)

    # (a) CLI route, currently unavailable/cli_not_verified -> the probe runs
    # exactly once, warming the route into `list_available_routes`, and the
    # dev is seated.
    monkeypatch.setattr(
        model_availability, "resolve_route_availability",
        lambda route_ids: {
            route_ids[0]: model_availability.RouteAvailability(
                route_ids[0], "claude_cli", False, "cli_not_verified")})
    monkeypatch.setattr(
        pm_reference, "list_available_routes",
        lambda: [{"route_id": "claude_cli.opus", "family": "opus",
                  "provider_class": "claude_cli"}])
    store_a = LedgerStore("probe-a")
    store_a.set_run_config(members=[
        {"id": "d1", "coding_role": "dev", "gateway_route_id": "claude_cli.sonnet",
         "model_mode": "single"}])
    prior_a = F._default_assign_dev_route("probe-a", "claude_cli.opus")
    assert prior_a == ["claude_cli.sonnet"]
    assert probe_calls == ["claude_cli"]
    member_a = store_a.get_run_config()["members"][0]
    assert member_a["gateway_route_id"] == "claude_cli.opus"

    # (b) route already reports available -> no probe.
    probe_calls.clear()
    monkeypatch.setattr(
        model_availability, "resolve_route_availability",
        lambda route_ids: {
            route_ids[0]: model_availability.RouteAvailability(
                route_ids[0], "claude_cli", True, "")})
    store_b = LedgerStore("probe-b")
    store_b.set_run_config(members=[
        {"id": "d1", "coding_role": "dev", "gateway_route_id": "claude_cli.sonnet",
         "model_mode": "single"}])
    prior_b = F._default_assign_dev_route("probe-b", "claude_cli.opus")
    assert prior_b == ["claude_cli.sonnet"]
    assert probe_calls == []
    member_b = store_b.get_run_config()["members"][0]
    assert member_b["gateway_route_id"] == "claude_cli.opus"

    # (c) non-CLI provider class -> availability is never even consulted.
    probe_calls.clear()

    def _boom(route_ids):
        raise AssertionError(
            "resolve_route_availability must not be called for a non-CLI route")

    monkeypatch.setattr(model_availability, "resolve_route_availability", _boom)
    monkeypatch.setattr(
        pm_reference, "list_available_routes",
        lambda: [{"route_id": "anthropic.claude-sonnet-4-6", "family": "sonnet",
                  "provider_class": "anthropic"}])
    store_c = LedgerStore("probe-c")
    store_c.set_run_config(members=[
        {"id": "d1", "coding_role": "dev", "gateway_route_id": "claude_cli.sonnet",
         "model_mode": "single"}])
    prior_c = F._default_assign_dev_route("probe-c", "anthropic.claude-sonnet-4-6")
    assert prior_c == ["claude_cli.sonnet"]
    assert probe_calls == []
    member_c = store_c.get_run_config()["members"][0]
    assert member_c["gateway_route_id"] == "anthropic.claude-sonnet-4-6"


def test_default_seed_workspace_over_a_real_ledger_store(
        tmp_path: pathlib.Path) -> None:
    """`_default_seed_workspace` is the seam's only production implementation
    and, before this test, had no coverage of its own -- everything else
    drives it through the `Fake`. Exercise it directly against a real
    `LedgerStore` + `CodingWorkspace` (the fixture-provided `ERRORTA_HOME`
    already points at `tmp_path`)."""
    import subprocess

    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    repo_dir = tmp_path / "src-repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
        cwd=repo_dir, check=True)

    store = LedgerStore("unit-seed")
    store.create_project(north_star="n", definition_of_done="d",
                         target="existing", repo_path=str(repo_dir))

    assert F._default_seed_workspace("unit-seed") is True
    assert CodingWorkspace("unit-seed", store).exists() is True
    assert F._default_seed_workspace("unit-seed") is False


def test_already_running_project_is_never_fought() -> None:
    fake = Fake()
    fake.store.state = {"status": "running"}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_project_busy" and fake.store.tasks == []
    # `assign_dev_route` durably rewrites `run_config` (`set_run_config`); a
    # busy pause has nothing to restore that with, so the team must not be
    # touched ahead of the busy check.
    assert fake.assigned == []
    assert "fix_team_model" not in [k for k, _ in out.events]


def test_an_unfixable_repo_pauses_for_a_human() -> None:
    fake = Fake()
    prof = _profile(fixable=False)
    out = _cycle(fake, prof=prof).step()
    assert out.code == "repo_not_fixable" and out.failed is False


def test_ambiguous_triage_with_no_pm_seam_pauses_and_never_guesses() -> None:
    fake = Fake()
    bundle = _bundle(stop_reason="stall:nothing-declared", stalled_probe_id="x")
    out = _cycle(fake, bundle=bundle).step()
    assert out.code == "triage_ambiguous" and fake.store.tasks == []
    assert out.events[0][1]["repo_id"] is None


def test_an_ambiguous_triage_takes_exactly_one_pm_turn_and_obeys_it() -> None:
    fake = Fake()
    fake.triage_replies = ['{"repo_id": "brain", "rationale": "the brain log is quiet"}']
    bundle = _bundle(stop_reason="stall:nothing-declared", stalled_probe_id="x")
    cyc = _cycle(fake, bundle=bundle)
    out = cyc.step()
    assert out.kind == "pending" and cyc.repo_id == "brain"
    assert out.events[0][1]["confidence"] == "ambiguous"
    assert fake.triage_replies == []            # exactly one turn


def test_a_pm_reply_naming_an_undeclared_repo_stays_ambiguous() -> None:
    fake = Fake()
    fake.triage_replies = ['{"repo_id": "../etc", "rationale": "no"}']
    bundle = _bundle(stop_reason="stall:nothing-declared", stalled_probe_id="x")
    out = _cycle(fake, bundle=bundle).step()
    assert out.code == "triage_ambiguous"


def test_a_start_run_failure_is_a_clean_pause_not_a_raise() -> None:
    fake = Fake()
    fake.start_raises = RuntimeError("engine down")
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_run_failed" and out.failed is True


# -- the delivered diff ---------------------------------------------------- #

def test_clean_stop_with_no_delivered_paths_is_not_a_fix() -> None:
    fake = Fake()
    fake.ws.changed = []
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_no_delivery" and out.failed is True
    assert fake.staged == []


def test_a_failed_run_with_no_delivery_says_the_run_failed() -> None:
    fake = Fake()
    fake.ws.changed = []
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.run_end_status = "failed"
    fake.store.state["status"] = "failed"
    out = _drive(cyc, fake)
    assert out.code == "fix_run_failed"


@pytest.mark.parametrize("path,human", [
    ("senditai_ng/safety/limits.py", True),
    ("senditai_ng/dispatch/killswitch_state.py", True),
    ("errorta_liverun/supervisor.py", True),
    ("python/errorta_liverun/supervisor.py", False),   # prefix, not a substring
    ("senditai_ng/agent/plan.py", False),
    ("./senditai_ng/safety/limits.py", True),          # normalized first
    ("senditai_ng/safety", False),                     # the dir itself is not the tree
])
def test_guarded_paths_force_human_only(path, human, tmp_path) -> None:
    assert is_human_only_diff([path], profiles_dir=tmp_path) is human


def test_absolute_profile_path_is_human_only(tmp_path) -> None:
    assert is_human_only_diff([str(tmp_path / "osrs.yaml")], profiles_dir=tmp_path) is True


def test_any_path_we_cannot_normalize_is_human_only(tmp_path) -> None:
    assert is_human_only_diff(["../../etc/passwd"], profiles_dir=tmp_path) is True
    assert is_human_only_diff(["/etc/passwd"], profiles_dir=tmp_path) is True
    assert is_human_only_diff([""], profiles_dir=tmp_path) is True


def test_a_path_escaping_the_repo_is_refused_not_merely_gated() -> None:
    assert escapes_repo(["../../etc/passwd"]) is True
    assert escapes_repo(["/etc/passwd"]) is True
    assert escapes_repo(["a/../../b"]) is True
    assert escapes_repo(["senditai_ng/safety/limits.py"]) is False


def test_a_delivered_path_outside_the_repo_refuses_the_whole_cycle() -> None:
    fake = Fake()
    fake.ws.changed = ["senditai_ng/ok.py", "../../etc/passwd"]
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_unsafe_paths" and out.failed is True
    assert fake.staged == []


def test_guarded_prefixes_are_declared_not_invented() -> None:
    assert "errorta_liverun/" in GUARDED_PATH_PREFIXES


# -- the accept path ------------------------------------------------------- #

def test_accept_staging_carries_the_human_only_flag() -> None:
    fake = Fake()
    fake.ws.changed = ["senditai_ng/safety/limits.py"]
    cyc = _cycle(fake)
    _drive(cyc, fake)
    verb, args, thread_ts, channel = fake.staged[0]
    assert verb == "accept_live_fix" and args["human_only"] is True
    assert args["project_id"] == "senditai-ng" and args["repo_id"] == "brain"
    assert args["run_id"] == "lr-1" and args["task_id"] == "t1"
    assert args["changed_paths"] == ["senditai_ng/safety/limits.py"]
    assert channel == "C-live" and thread_ts == ""


def test_a_plain_diff_is_not_human_only() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert fake.staged[0][1]["human_only"] is False
    assert out.kind == "deployed"


def test_declined_confirmation_counts_a_failed_cycle() -> None:
    fake = Fake()
    fake.confirm_state = "declined"
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_declined" and out.failed is True
    assert fake.actions == []                   # nothing deployed


def test_an_unanswered_confirmation_times_out_as_a_failed_cycle() -> None:
    fake = Fake()
    fake.confirm_state = "pending"
    cyc = _cycle(fake, accept_timeout_s=1800)
    cyc.step(); cyc.step(); cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31); cyc.step()           # watch -> stage
    cyc.step()                                   # stage -> await
    assert fake.staged and cyc.step().kind == "pending"   # waiting, not fired
    fake.clock.advance(1801)
    out = cyc.step()
    assert out.code == "fix_declined" and out.failed is True
    assert out.detail == "timeout"


def test_a_blocked_merge_gate_stages_nothing_and_merges_nothing() -> None:
    fake = Fake()
    fake.merge_gate = False
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_gate_blocked" and out.failed is True
    assert fake.staged == [] and fake.actions == []


def test_a_gate_that_closes_between_staging_and_approval_never_deploys() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31); cyc.step()          # watch -> stage
    cyc.step()                                  # stage: gate open, confirmation staged
    assert fake.staged
    fake.merge_gate = False
    fake.clock.advance(31)
    out = cyc.step()
    assert out.code == "fix_gate_blocked" and fake.actions == []


def test_the_accepted_event_names_the_repo_and_the_head() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    accepted = [d for k, d in out.events if k == "fix_accepted"][0]
    assert accepted["repo_id"] == "brain" and accepted["head"] == "h1"
    assert accepted["delivered_to"] == "/r/senditai-ng"


# -- deploy ---------------------------------------------------------------- #

def test_deploy_step_failure_pauses_and_never_reports_a_deployed_cycle() -> None:
    fake = Fake()
    fake.action_results["/usr/bin/rsync"] = StepResult(False, "a", "b", exit_code=23,
                                                       stderr_tail="rsync: partial")
    out = _drive(_cycle(fake), fake)
    assert out.code == "deploy_failed:rsync" and out.failed is True
    assert [k for k, _ in out.events][-1] == "deploy_step"


def test_a_ban_signal_during_deploy_pauses_the_profile() -> None:
    fake = Fake()
    fake.action_results["/usr/bin/rsync"] = StepResult(
        True, "a", "b", exit_code=0, stdout_tail="Login failed: Account is banned")
    out = _drive(_cycle(fake), fake)
    assert out.code == "ban_signal" and fake.banned == ["deploy:rsync"]


def test_a_repo_with_no_deploy_steps_still_completes_the_cycle() -> None:
    fake = Fake()
    out = _drive(_cycle(fake, prof=_profile(deploy=False)), fake)
    assert out.kind == "deployed" and fake.actions == []


def test_a_deploy_check_that_never_passes_times_the_step_out() -> None:
    fake = Fake()
    act = P.Action("local", {"argv": ("/usr/bin/rsync", "-az", "/src/", "h:dst/"), "cwd": None})
    prof = _profile()
    repo = prof.repos[0]
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, prof.launch, prof.watch,
                     prof.evidence, prof.teardown, prof.caps, prof.ban_signals,
                     (P.RepoDef(repo.id, repo.path, repo.errorta_project, True,
                                repo.classify,
                                (P.Step("rsync", act, P.Check("file_exists", "/nope"), 60),)),
                      *prof.repos[1:]), prof.fix_loop)
    fake.check_ok = False
    out = _drive(_cycle(fake, prof=prof), fake, limit=80)
    assert out.code == "deploy_failed:rsync:check_timeout"
    assert fake.actions == ["/usr/bin/rsync"]   # the action ran exactly once


# -- invariants ------------------------------------------------------------ #

def test_no_merge_gate_bypass_parameter_anywhere_in_the_package() -> None:
    src = pathlib.Path(errorta_liverun.__file__).parent
    hits = [p.name for p in src.glob("*.py") if "overr" + "ide" in p.read_text()]
    assert hits == []


def test_fixloop_imports_no_engine_package_at_module_level() -> None:
    """Every engine touch is a lazily-resolved seam: `errorta_liverun` must
    import with neither the council package nor the FastAPI route layer."""
    import errorta_liverun.fixloop as F

    tree = ast.parse(pathlib.Path(F.__file__).read_text())
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for n in names:
            assert not n.startswith(("errorta_app.routes", "errorta_council",
                                     "errorta_slack")), n


def test_every_pause_code_the_driver_can_emit_is_declared() -> None:
    from errorta_liverun.fixloop import PAUSE_CODES

    assert {"fix_no_gate", "fix_project_busy", "fix_no_delivery", "fix_declined",
            "fix_gate_blocked", "fix_idle", "fix_run_failed", "triage_ambiguous",
            "repo_not_fixable", "fix_unsafe_paths",
            "fix_project_not_existing"} <= set(PAUSE_CODES)


# -- the start race -------------------------------------------------------- #

def test_a_continue_start_is_not_mistaken_for_a_finished_run() -> None:
    """`continue_` starts FROM "stopped": until the run state has actually said
    "running", a terminal status is the status the project already had."""
    fake = Fake()
    fake.store.state = {"status": "stopped"}
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    assert fake.started == [("senditai-ng", {"resume": False, "continue_": True})]
    fake.store.state["status"] = "stopped"       # the engine has not picked it up yet
    fake.clock.advance(31)
    assert cyc.step().kind == "pending"
    assert fake.ws.calls == []                   # nothing read: no diff exists yet
    fake.store.state["status"] = "running"
    fake.clock.advance(31); cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31); cyc.step()
    assert fake.ws.calls == [("master", "h0")]


def test_a_run_nothing_ever_picks_up_fails_the_cycle() -> None:
    fake = Fake()
    fake.start_sets_running = False
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.store.state["status"] = "idle"          # never became `running`
    out = None
    for _ in range(6):
        fake.clock.advance(31)
        out = cyc.step()
        if out.kind == "paused":
            break
    assert out.code == "fix_run_failed" and out.failed is True
    assert out.detail == "never_started:idle"


def test_a_refused_start_ends_the_cycle_instead_of_watching_nothing() -> None:
    fake = Fake()
    fake.start_result = {"status": "refused", "detail": "no active focus"}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_run_failed" and out.detail == "refused"
    assert [k for k, _ in out.events][-1] == "fix_run"


def test_an_already_running_project_reported_by_the_engine_is_not_fought() -> None:
    fake = Fake()
    fake.start_result = {"status": "already_running"}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_project_busy" and out.failed is False


# -- a staged acceptance is never left behind ------------------------------ #

def _pending_cids(fake: Fake) -> list[str]:
    return [cid for cid, r in fake.confirmations.items() if r["state"] == "pending"]


def test_an_unanswered_acceptance_is_withdrawn_not_left_pending() -> None:
    """The autopilot sweep fires on PENDING records. A cycle that stopped
    waiting must take its own button off the table, or the merge happens
    minutes after the cycle paused."""
    fake = Fake()
    fake.confirm_state = "pending"
    cyc = _cycle(fake, accept_timeout_s=1500)
    cyc.step(); cyc.step(); cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31); cyc.step()
    cyc.step()
    cid = fake.confirmations and list(fake.confirmations)[0]
    assert _pending_cids(fake) == [cid]
    fake.clock.advance(1501)
    out = cyc.step()
    assert out.code == "fix_declined"
    assert _pending_cids(fake) == []
    assert fake.resolved == [(cid, "declined")]
    assert ("fix_accept_withdrawn", {"cid": cid, "claimed": True,
                                     "decision": "declined"}) in out.events


def test_a_declined_acceptance_is_not_withdrawn_twice() -> None:
    fake = Fake()
    fake.confirm_state = "declined"
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_declined"
    withdrawn = [d for k, d in out.events if k == "fix_accept_withdrawn"]
    assert len(withdrawn) == 1 and withdrawn[0]["claimed"] is False   # already resolved


def test_an_approved_acceptance_is_never_withdrawn() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    assert out.kind == "deployed"
    assert fake.resolved == [] and [k for k, _ in out.events if k == "fix_accept_withdrawn"] == []


def test_a_deploy_failure_after_approval_withdraws_nothing() -> None:
    fake = Fake()
    fake.action_results["/usr/bin/rsync"] = StepResult(False, "a", "b", exit_code=23)
    out = _drive(_cycle(fake), fake)
    assert out.code == "deploy_failed:rsync" and fake.resolved == []


def test_the_accept_timeout_can_never_outlast_slacks_own_sweep() -> None:
    """Slack's `sweep_timeouts` claims a pending confirmation after 30 min. A
    profile that asks for longer gets the bound, not the ask."""
    from errorta_liverun.fixloop import DEFAULT_ACCEPT_TIMEOUT_S

    assert DEFAULT_ACCEPT_TIMEOUT_S < 30 * 60
    cyc = _cycle(Fake(), accept_timeout_s=99_999)
    assert cyc._accept_timeout_s == DEFAULT_ACCEPT_TIMEOUT_S
    assert _cycle(Fake(), accept_timeout_s=60)._accept_timeout_s == 60


# -- abort ------------------------------------------------------------------ #

def test_abort_cancels_the_dev_run_and_withdraws_the_button() -> None:
    fake = Fake()
    fake.confirm_state = "pending"
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.finish_run("stopped")
    fake.clock.advance(31); cyc.step(); cyc.step()      # staged, awaiting
    assert _pending_cids(fake)
    fake.store.state["status"] = "running"              # a turn is still going
    out = cyc.abort("operator_stop")
    assert out.kind == "aborted" and out.failed is False
    assert fake.store.state["cancel_requested"] is True
    assert _pending_cids(fake) == []
    detail = dict(out.events)["fix_aborted"]
    assert detail["run_cancelled"] is True and detail["accept_withdrawn"] is True
    assert detail["repo_id"] == "brain" and detail["at"] == "await"


def test_abort_before_a_run_started_cancels_nothing() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step()                                          # triage only
    out = cyc.abort("operator_stop")
    assert out.kind == "aborted"
    assert fake.store.state.get("cancel_requested") is not True
    assert fake.started == [] and dict(out.events)["fix_aborted"]["run_cancelled"] is False


def test_abort_does_not_re_cancel_a_run_that_already_stopped() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.finish_run("stopped")
    out = cyc.abort("sidecar_shutdown")
    assert fake.store.state.get("cancel_requested") is not True
    assert dict(out.events)["fix_aborted"]["run_cancelled"] is False


def test_abort_is_idempotent_and_the_cycle_stays_aborted() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    first = cyc.abort("operator_stop")
    second = cyc.abort("operator_stop")
    assert first.events and second.events == []
    assert cyc.step().kind == "aborted"
    assert len(fake.resolved) <= 1


# -- approved is a decision; merged is a fact ------------------------------- #

def test_an_approval_that_left_the_branch_untouched_is_not_a_fix() -> None:
    fake = Fake()
    fake.ws.head_value = "h0"           # the delivered work is gone from master
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()
    fake.store.state["status"] = "stopped"          # finish WITHOUT moving head
    out = None
    for _ in range(6):
        fake.clock.advance(31)
        out = cyc.step()
        if out.kind == "paused":
            break
    assert out.code == "fix_accept_unverified" and out.failed is True
    assert fake.actions == []


def test_a_recorded_gate_block_is_believed_over_the_re_check() -> None:
    fake = Fake()
    fake.accept_outcome = {"status": "gate_blocked", "gate": {"blockers": ["tests"]}}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_gate_blocked" and fake.actions == []


def test_a_recorded_accept_names_what_verified_it() -> None:
    fake = Fake()
    fake.accept_outcome = {"status": "accepted", "delivered_to": "/r/senditai-ng"}
    out = _drive(_cycle(fake), fake)
    assert out.kind == "deployed"
    accepted = [d for k, d in out.events if k == "fix_accepted"][0]
    assert accepted["verified_by"] == "accepted"


def test_an_unknown_recorded_outcome_pauses_rather_than_deploying() -> None:
    fake = Fake()
    fake.accept_outcome = {"status": "refused", "detail": "no worktree"}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_accept_unverified" and out.detail == "refused"


@pytest.mark.parametrize("reason", ["conflicts", "unsafe_path"])
def test_a_merge_that_refused_to_apply_never_deploys(reason: str) -> None:
    """The `applied: False` row `accept_live_fix` now writes down. `merge_back`
    is fail-closed on a conflicting or traversing path and RETURNS that fact
    rather than raising, so before this row existed the effect delivered, said
    "accepted", and the cycle deployed and relaunched a fix that never landed.
    The head DID move here (the dev run committed) — only the recorded outcome
    can tell the cycle the operator's tree is untouched."""
    fake = Fake()
    fake.accept_outcome = {"status": "error", "reason": reason}

    out = _drive(_cycle(fake), fake)

    assert out.kind == "paused"
    assert out.code == "fix_accept_unverified" and out.detail == "error"
    assert out.failed is True
    assert fake.actions == []                   # nothing deployed, nothing relaunched


def test_a_delivery_error_pauses_the_cycle_too() -> None:
    """The merge landed but nothing reached the operator. Deploying would ship
    a tree the delivery never wrote."""
    fake = Fake()
    fake.accept_outcome = {"status": "delivery_error", "reason": "OSError"}

    out = _drive(_cycle(fake), fake)

    assert out.code == "fix_accept_unverified" and out.detail == "delivery_error"
    assert fake.actions == []


# -- a project with no repository to merge into ----------------------------- #


@pytest.mark.parametrize("target", ["new", ""])
def test_a_project_that_is_not_an_existing_repo_never_files_a_task(target: str) -> None:
    """A `new`-target project's accept returns the worktree root and delivery
    exports a folder: `repo.path` never changes, so the deploy steps would rsync
    a tree the fix never touched. Refused before a dev run is ever spent — and
    an unreadable target (`""`) is refused the same way, fail-closed."""
    fake = Fake()
    fake.store.target = target

    out = _cycle(fake).step()

    assert out.kind == "paused" and out.code == "fix_project_not_existing"
    assert out.failed is False                  # it never started; nothing was spent
    assert fake.store.tasks == [] and fake.started == []
    assert target or "unreadable" in out.detail


def test_a_ledger_that_cannot_answer_the_target_question_is_refused() -> None:
    fake = Fake()

    def _boom():
        raise RuntimeError("project row is gone")

    fake.store.get_project = _boom

    out = _cycle(fake).step()

    assert out.code == "fix_project_not_existing" and fake.store.tasks == []


def test_an_existing_target_project_proceeds_as_before() -> None:
    fake = Fake()
    assert fake.store.target == "existing"

    assert _drive(_cycle(fake), fake).kind == "deployed"


# --- the production default behind `accept_outcome_fn` --------------------- #
#
# `ws.head()` does not move when `deliver` copies the merged tree out, so the
# head check in `_verify_accepted` is a floor, not proof that the operator's
# files changed. The decision row `accept_live_fix` writes IS that proof --
# which is worth nothing while the seam behind it defaults to `None` and the
# cycle never reads it. These pin the wiring, not the shape.


def test_the_accept_outcome_seam_has_a_production_default() -> None:
    assert F.FixDeps().accept_outcome_fn is None      # still injectable
    assert F.FixDeps().accept_outcome("nope") is None  # and safe with nothing on disk


def test_the_default_reads_the_row_the_effect_recorded(monkeypatch) -> None:
    class _Store:
        def list_decisions(self) -> list[dict]:
            return [
                {"choice": "accept_live_fix", "run_id": "r-0", "status": "gate_blocked"},
                {"choice": "something_else", "run_id": "r-1", "status": "accepted"},
                {"choice": "accept_live_fix", "run_id": "r-1", "status": "accepted",
                 "repo_id": "brain", "delivered_to": "/r/senditai-ng"},
            ]

    monkeypatch.setattr(F, "_default_get_confirmation", lambda cid: {
        "id": cid, "verb": "accept_live_fix", "state": "approved",
        "args": {"project_id": "p1", "run_id": "r-1"}})
    monkeypatch.setattr(F, "_default_ledger_factory", lambda pid: _Store())

    out = F.FixDeps().accept_outcome("c-1")

    assert out["status"] == "accepted" and out["delivered_to"] == "/r/senditai-ng"


def test_the_default_ignores_a_row_from_another_run(monkeypatch) -> None:
    """A project fixed twice in a day has two rows. Reading the wrong one would
    deploy this cycle on the strength of the previous cycle's merge."""
    class _Store:
        def list_decisions(self) -> list[dict]:
            return [{"choice": "accept_live_fix", "run_id": "r-0", "status": "accepted"}]

    monkeypatch.setattr(F, "_default_get_confirmation", lambda cid: {
        "args": {"project_id": "p1", "run_id": "r-1"}})
    monkeypatch.setattr(F, "_default_ledger_factory", lambda pid: _Store())

    assert F.FixDeps().accept_outcome("c-1") is None


def test_the_default_survives_a_ledger_it_cannot_read(monkeypatch) -> None:
    def _boom(pid):
        raise RuntimeError("no such project")

    monkeypatch.setattr(F, "_default_get_confirmation", lambda cid: {
        "args": {"project_id": "p1", "run_id": "r-1"}})
    monkeypatch.setattr(F, "_default_ledger_factory", _boom)

    # None means "nobody recorded it", which falls back to the workspace checks
    # -- the same place the cycle was before this seam had a default at all.
    assert F.FixDeps().accept_outcome("c-1") is None


def test_a_deploy_check_is_told_when_its_step_started_not_when_it_polled() -> None:
    """`file_mtime_newer` compares `step_start` against a file's mtime. A start
    stamp that moved with every poll would always be newer than the file the
    check is waiting for, so the check could never pass."""
    fake = Fake()
    act = P.Action("local", {"argv": ("/usr/bin/rsync", "-az", "/src/", "h:dst/"), "cwd": None})
    prof = _profile()
    repo = prof.repos[0]
    prof = P.Profile(prof.name, prof.hosts, prof.tunnels, prof.launch, prof.watch,
                     prof.evidence, prof.teardown, prof.caps, prof.ban_signals,
                     (P.RepoDef(repo.id, repo.path, repo.errorta_project, True, repo.classify,
                                (P.Step("rsync", act, P.Check("file_exists", "/nope"), 600),)),
                      *prof.repos[1:]), prof.fix_loop)
    fake.check_ok = False
    cyc = _cycle(fake, prof=prof)
    for _ in range(10):
        out = cyc.step()
        if out.kind in ("paused", "deployed"):
            break
        if fake.started and fake.store.state.get("status") == "running":
            fake.finish_run()
        fake.clock.advance(31)
    started_at = fake.check_starts[0]
    assert len(fake.check_starts) >= 3
    assert set(fake.check_starts) == {started_at}          # one stamp, not one per poll
    assert started_at <= fake.clock.t - 60                 # ... and it is the STEP's start



def test_the_fix_is_filed_as_the_operative_focus_before_the_task() -> None:
    """Live 2026-08-22 (run b8370d): a task with no Focus let the PM plan the
    North Star and scaffold an existing repo from scratch. The fix cycle must
    set the fix as the team's one active goal, pointing at the task."""
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step()                                  # triage
    out = cyc.step()                            # task
    store = fake.store
    assert store.focuses and store.focuses[0]["origin"] == "liverun"
    assert store.focuses[0]["title"] == store.tasks[0]["title"]
    assert "do not scaffold" in store.focuses[0]["body"]
    assert dict(out.events)["fix_task"]["focus_id"] == "f1"
