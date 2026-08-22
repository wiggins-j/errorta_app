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

    def add_task(self, **kw):
        self.tasks.append(kw)
        return SimpleNamespace(task_id=f"t{len(self.tasks)}")

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
        self.action_results: dict[str, StepResult] = {}
        self.actions: list[str] = []
        self.checks: list[str] = []
        self.check_ok = True
        self.banned: list[str] = []
        self.triage_replies: list[str] = []
        self.start_raises: Exception | None = None
        self.run_end_status = "stopped"
        self.start_result: dict = {"status": "started"}
        self.start_sets_running = True

    # -- seams ---------------------------------------------------------- #
    def deps(self) -> FixDeps:
        return FixDeps(
            ledger_factory=lambda pid: self.store,
            workspace_factory=lambda pid: self.ws,
            gate_available_fn=lambda store: self.gate,
            merge_gate_ok_fn=lambda store, ws: self.merge_gate,
            start_run_fn=self._start_run,
            team_log_fn=lambda store: list(self.store.log),
            stage_confirmation_fn=self._stage,
            get_confirmation_fn=lambda cid: self.confirmations.get(cid),
            triage_fn=(lambda prompt, project_id, route: self.triage_replies.pop(0))
            if self.triage_replies else None,
            bound_channel_fn=lambda pid: "C-live",
        )

    def _start_run(self, project_id: str, *, resume: bool, continue_: bool) -> dict:
        if self.start_raises is not None:
            raise self.start_raises
        self.started.append((project_id, {"resume": resume, "continue_": continue_}))
        if self.start_sets_running and self.start_result.get("status") == "started":
            self.store.state["status"] = "running"
        return dict(self.start_result)

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
            fake.store.state["status"] = fake.run_end_status
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
        "fix_triage", "fix_task", "fix_run", "fix_accept_staged", "fix_accepted",
        "deploy_step"]
    assert cyc.repo_id == "brain" and cyc.task_id == "t1"


def test_the_run_goes_terminal_before_the_diff_is_ever_read() -> None:
    fake = Fake()
    cyc = _cycle(fake)
    cyc.step(); cyc.step(); cyc.step()          # triage, task, run
    assert fake.ws.calls == []                  # still running: nothing read
    fake.store.state["status"] = "stopped"
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
    fake.store.state["status"] = "stopped"
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


def test_already_running_project_is_never_fought() -> None:
    fake = Fake()
    fake.store.state = {"status": "running"}
    out = _drive(_cycle(fake), fake)
    assert out.code == "fix_project_busy" and fake.store.tasks == []


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
    fake.store.state["status"] = "stopped"
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
    fake.store.state["status"] = "stopped"
    fake.clock.advance(31); cyc.step()          # watch -> stage
    cyc.step()                                  # stage: gate open, confirmation staged
    assert fake.staged
    fake.merge_gate = False
    out = cyc.step()
    assert out.code == "fix_gate_blocked" and fake.actions == []


def test_the_accepted_event_names_the_repo_and_the_head() -> None:
    fake = Fake()
    out = _drive(_cycle(fake), fake)
    accepted = [d for k, d in out.events if k == "fix_accepted"][0]
    assert accepted["repo_id"] == "brain" and accepted["head"] == "h0"
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
            "repo_not_fixable", "fix_unsafe_paths"} <= set(PAUSE_CODES)


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
    fake.store.state["status"] = "stopped"
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
