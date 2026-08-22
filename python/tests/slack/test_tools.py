from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from errorta_council.coding.ledger import LedgerStore, ProjectNotFound
from errorta_council.coding.runtime_process import RuntimeProcessError, RuntimeProcessManager
from errorta_council.coding.workspace import CodingWorkspace
from errorta_slack import store, tools


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- Fakes -------------------------------------------------------------


class FakeTask:
    def __init__(self, task_id: str, state: str = "todo", title: str = "") -> None:
        self.task_id = task_id
        self.state = state
        self.title = title


# The team's configured PM, as it is persisted in the project's run config.
# `propose_next_goal` must resolve its model route from HERE and nowhere else.
_PM_MEMBER = {
    "member_id": "m-pm", "role": "answerer", "enabled": True,
    "gateway_route_id": "local.pm-route", "provider_kind": "cli",
    "metadata": {"coding_role": "pm"},
}


class FakeLedgerStore:
    """Stub matching the surface build_team_log/attention.list_open touch."""

    def __init__(self, project_id: str, tmp_path: Path) -> None:
        self.project_id = project_id
        self.dir = tmp_path / f"ledger-{project_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.added_tasks: list[dict[str, Any]] = []
        self.tasks: list[FakeTask] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.raise_on_update = False
        self._next_id = 0
        self.run_state: dict[str, Any] = {
            "status": "idle", "stop_reason": None, "started_at": None,
            "ended_at": None, "cancel_requested": False,
            "last_error": None, "counters": None,
        }

    def list_tasks(self) -> list[Any]:
        return list(self.tasks)

    def update_task(self, task_id: str, **patch: Any) -> Any:
        from errorta_council.coding.ledger import LedgerError

        if self.raise_on_update or not any(t.task_id == task_id for t in self.tasks):
            raise LedgerError(f"unknown task: {task_id}")
        self.updates.append((task_id, dict(patch)))
        for t in self.tasks:
            if t.task_id == task_id:
                t.state = str(patch.get("state", t.state))
                return t
        return None

    def list_turns(self) -> list[dict[str, Any]]:
        return []

    def list_decisions(self) -> list[dict[str, Any]]:
        return []

    def get_project(self) -> Any:
        raise RuntimeError("no project")

    def add_task(self, *, title: str, role: str, detail: str = "",
                 task_type: str = "implementation", **_: Any) -> FakeTask:
        self.added_tasks.append(
            {"title": title, "role": role, "detail": detail, "task_type": task_type}
        )
        self._next_id += 1
        return FakeTask(f"t-{self._next_id}")

    def get_run_state(self) -> dict[str, Any]:
        return dict(self.run_state)

    def set_run_state(self, **patch: Any) -> dict[str, Any]:
        self.run_state.update(patch)
        return dict(self.run_state)


def _deps(tmp_path: Path, **overrides: Any) -> tools.ToolDeps:
    ledger_stores: dict[str, FakeLedgerStore] = {}

    def ledger_factory(project_id: str) -> FakeLedgerStore:
        if project_id not in ledger_stores:
            ledger_stores[project_id] = FakeLedgerStore(project_id, tmp_path)
        return ledger_stores[project_id]

    kwargs: dict[str, Any] = {
        "store": store,
        "ledger_factory": ledger_factory,
        "launch_fn": lambda project_id: None,
        "publish_fn": lambda args: {"pr_url": "https://example.invalid/pr/1"},
        "pm_changes_mod": _FakePmChanges(),
    }
    kwargs.update(overrides)
    deps = tools.ToolDeps(**kwargs)
    deps._ledger_stores = ledger_stores  # type: ignore[attr-defined]
    return deps


class _FakePmChanges:
    def __init__(self) -> None:
        self.accepted: list[tuple[Any, str]] = []
        self.declined: list[tuple[Any, str]] = []

    def accept(self, ledger_store: Any, change_id: str) -> None:
        self.accepted.append((ledger_store, change_id))

    def decline(self, ledger_store: Any, change_id: str) -> None:
        self.declined.append((ledger_store, change_id))


# --- Catalog invariants --------------------------------------------------


def test_catalog_trust_values_are_r_or_c() -> None:
    for verb, spec in tools.TOOL_CATALOG.items():
        assert spec["trust"] in ("R", "C"), verb
        assert spec["summary"]


# --- project_status ------------------------------------------------------


def test_project_status_wires_team_log_and_attention(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "project_status", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert "tasks" in result
    assert "blockers" in result
    assert isinstance(result["tasks"], list)
    assert isinstance(result["blockers"], list)


def test_project_status_unbound_channel_raises() -> None:
    deps = _deps(Path("."))
    with pytest.raises(tools.ToolError) as exc:
        tools.dispatch(
            "project_status", {}, channel_id="C-unbound", thread_ts="t1", deps=deps,
        )
    assert exc.value.code == "no_project_bound"


# --- queue_bugs ------------------------------------------------------------


def test_queue_bugs_calls_add_task_for_each_with_valid_role(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "queue_bugs", {"bugs": ["a", "b", "c"]},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    ledger_store = deps._ledger_stores["proj-a"]  # type: ignore[attr-defined]
    assert len(ledger_store.added_tasks) == 3
    for call in ledger_store.added_tasks:
        assert call["role"] == "dev"
        assert call["task_type"] == "implementation"
    assert result["task_ids"] == ["t-1", "t-2", "t-3"]


# --- Injection guard: publish_pr -------------------------------------------


def test_publish_pr_without_block_actions_stages_and_does_not_publish(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    deps = _deps(tmp_path, publish_fn=lambda args: calls.append(args) or {"pr_url": "x"})

    result = tools.dispatch(
        "publish_pr", {"title": "hello"},
        channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert "confirmation_id" in result
    assert calls == []

    staged = store.get_confirmation(result["confirmation_id"])
    assert staged is not None
    assert staged["verb"] == "publish_pr"
    assert staged["args"] == {"title": "hello"}


def test_publish_pr_with_block_actions_publishes(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    deps = _deps(tmp_path, publish_fn=lambda args: calls.append(args) or {"pr_url": "x"})

    result = tools.dispatch(
        "publish_pr", {"title": "hello"},
        channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "published"
    assert calls == [{"title": "hello"}]


# --- Injection guard: resolve_decision -------------------------------------


def test_resolve_decision_without_block_actions_stages_and_does_not_resolve(
    tmp_path: Path,
) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    fake_pm = deps.pm_changes_mod

    result = tools.dispatch(
        "resolve_decision", {"change_id": "chg-1", "decision": "accept"},
        channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert fake_pm.accepted == []
    assert fake_pm.declined == []


def test_resolve_decision_with_block_actions_resolves(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    fake_pm = deps.pm_changes_mod

    result = tools.dispatch(
        "resolve_decision", {"change_id": "chg-1", "decision": "accept"},
        channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "resolved"
    assert len(fake_pm.accepted) == 1
    assert fake_pm.accepted[0][1] == "chg-1"
    assert fake_pm.declined == []


# --- Injection guard: spend_cloud ------------------------------------------


def test_spend_cloud_without_block_actions_stages_and_does_not_authorize(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "spend_cloud", {"amount": 5, "reason": "extra cloud pass"},
        channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert result.get("status") != "authorized"


def test_spend_cloud_with_block_actions_authorizes(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "spend_cloud", {"amount": 5, "reason": "extra cloud pass"},
        channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "authorized"
    assert result["amount"] == 5


# --- A confirmation id parsed from chat text must never reach the effect --


def test_confirmation_id_alone_cannot_trigger_effect_without_block_actions(
    tmp_path: Path,
) -> None:
    """Even if an attacker supplies a real staged confirmation_id in args, a
    dispatch call without confirmed_via="block_actions" must still stage
    (not execute) — the marker is the only thing that authorizes execution."""
    calls: list[dict[str, Any]] = []
    deps = _deps(tmp_path, publish_fn=lambda args: calls.append(args) or {})

    first = tools.dispatch(
        "publish_pr", {"title": "hello"},
        channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )
    cid = first["confirmation_id"]

    result = tools.dispatch(
        "publish_pr", {"title": "hello", "confirmation_id": cid},
        channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert calls == []


# --- Fail-closed: unknown verb ---------------------------------------------


def test_unknown_verb_raises_tool_not_allowed_naming_catalog(tmp_path: Path) -> None:
    deps = _deps(tmp_path)

    with pytest.raises(tools.ToolError) as exc:
        tools.dispatch("delete_everything", {}, channel_id="C1", thread_ts="t1", deps=deps)

    assert exc.value.code == "tool_not_allowed"
    message = str(exc.value)
    for verb in tools.TOOL_CATALOG:
        assert verb in message


# --- launch_runtime ----------------------------------------------------


def test_launch_runtime_empty_state_does_not_start(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    asked: list[str] = []

    def launch_fn(project_id: str) -> None:
        asked.append(project_id)
        return None

    deps = _deps(tmp_path, launch_fn=launch_fn)

    result = tools.dispatch(
        "launch_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result == {"status": "empty"}
    assert asked == ["proj-a"]


def test_launch_runtime_running_returns_loopback_url_and_no_public_note(
    tmp_path: Path,
) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(
        tmp_path,
        launch_fn=lambda project_id: {"host": "127.0.0.1", "port": 41234},
    )

    result = tools.dispatch(
        "launch_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "running"
    assert result["url"] == "http://127.0.0.1:41234"
    assert "no public" in result["note"].lower()


# --- resolve_decision: reject a garbage decision value ---------------------


def test_resolve_decision_rejects_unknown_decision_value(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    with pytest.raises(tools.ToolError) as exc:
        tools.dispatch(
            "resolve_decision", {"change_id": "chg-1", "decision": "maybe"},
            channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
        )

    assert exc.value.code == "invalid_decision"
    fake_pm = deps.pm_changes_mod
    assert fake_pm.accepted == []
    assert fake_pm.declined == []


# --- The DEFAULT launch/stop runtime path (real, lazily-imported seam) -----
#
# These exercise `_default_launch_fn` and `stop_runtime`'s own default
# integration with RuntimeProcessManager directly (monkeypatched so no real
# process starts) — the path every other test in this file bypasses by
# injecting a fake `launch_fn`.


class _FakeProfile:
    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id


class _FakeSession:
    def __init__(self, allocated_ports: list[int]) -> None:
        self.allocated_ports = allocated_ports


class _FakeRStore:
    def __init__(self, profiles: list[_FakeProfile]) -> None:
        self._profiles = profiles

    def list_profiles(self) -> list[_FakeProfile]:
        return self._profiles


class _FakeManager:
    def __init__(
        self,
        profiles: list[_FakeProfile],
        *,
        start_result: _FakeSession | None = None,
        start_exc: Exception | None = None,
        stop_exc: Exception | None = None,
    ) -> None:
        self.rstore = _FakeRStore(profiles)
        self._start_result = start_result
        self._start_exc = start_exc
        self._stop_exc = stop_exc
        self.stopped_profile_id: str | None = None

    def start(self, profile_id: str, auto_setup: bool = False) -> _FakeSession:
        if self._start_exc is not None:
            raise self._start_exc
        assert self._start_result is not None
        return self._start_result

    def stop(self, profile_id: str) -> None:
        if self._stop_exc is not None:
            raise self._stop_exc
        self.stopped_profile_id = profile_id


def test_default_launch_fn_running_uses_allocated_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mgr = _FakeManager(
        [_FakeProfile("p1")], start_result=_FakeSession([54321]),
    )
    monkeypatch.setattr(RuntimeProcessManager, "for_project", lambda project_id: fake_mgr)

    result = tools._default_launch_fn("proj-x")

    assert result == {"host": "127.0.0.1", "port": 54321}


def test_default_launch_fn_project_not_found_is_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(project_id: str) -> None:
        raise ProjectNotFound(project_id)

    monkeypatch.setattr(RuntimeProcessManager, "for_project", _raise)

    result = tools._default_launch_fn("proj-missing")

    assert result is not None
    assert result["status"] == "empty"
    assert "reason" in result


def test_default_launch_fn_start_failure_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_mgr = _FakeManager(
        [_FakeProfile("p1")], start_exc=RuntimeProcessError("setup_required"),
    )
    monkeypatch.setattr(RuntimeProcessManager, "for_project", lambda project_id: fake_mgr)

    result = tools._default_launch_fn("proj-x")

    assert result is not None
    assert result["status"] == "error"
    assert "detail" in result


def test_launch_runtime_default_path_running_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole dispatch("launch_runtime", ...) chain, with the REAL default
    launch_fn (not a test fake) — only RuntimeProcessManager is monkeypatched
    so no process actually starts."""
    store.bind_channel("C1", "proj-x")
    fake_mgr = _FakeManager(
        [_FakeProfile("p1")], start_result=_FakeSession([54321]),
    )
    monkeypatch.setattr(RuntimeProcessManager, "for_project", lambda project_id: fake_mgr)
    deps = tools.ToolDeps(store=store)

    result = tools.dispatch(
        "launch_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "running"
    assert result["url"] == "http://127.0.0.1:54321"


def test_stop_runtime_default_path_project_not_found_is_graceful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.bind_channel("C1", "proj-missing")

    def _raise(project_id: str) -> None:
        raise ProjectNotFound(project_id)

    monkeypatch.setattr(RuntimeProcessManager, "for_project", _raise)
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "stop_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "empty"
    assert "reason" in result


def test_stop_runtime_default_path_stop_failure_is_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.bind_channel("C1", "proj-x")
    fake_mgr = _FakeManager([_FakeProfile("p1")], stop_exc=RuntimeProcessError("no_worktree"))
    monkeypatch.setattr(RuntimeProcessManager, "for_project", lambda project_id: fake_mgr)
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "stop_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "detail" in result


def test_stop_runtime_default_path_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.bind_channel("C1", "proj-x")
    fake_mgr = _FakeManager([_FakeProfile("p1")])
    monkeypatch.setattr(RuntimeProcessManager, "for_project", lambda project_id: fake_mgr)
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "stop_runtime", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result == {"status": "stopped"}
    assert fake_mgr.stopped_profile_id == "p1"


# --- start_run / stop_run trust classes -------------------------------------


def test_start_run_is_trust_c_and_stop_run_is_trust_r() -> None:
    assert tools.TOOL_CATALOG["start_run"]["trust"] == "C"
    assert tools.TOOL_CATALOG["stop_run"]["trust"] == "R"


# --- start_run: THE injection guard test ------------------------------------


def test_start_run_without_block_actions_stages_and_never_calls_start_run_fn(
    tmp_path: Path,
) -> None:
    store.bind_channel("C1", "proj-a")
    calls: list[tuple[str, bool, bool]] = []

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        calls.append((project_id, resume, continue_))
        return {"started": True}

    deps = _deps(tmp_path, start_run_fn=start_run_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert "confirmation_id" in result
    assert calls == []


# --- start_run: mode is picked from the project's current run state --------


def test_start_run_idle_project_does_a_fresh_start(tmp_path: Path) -> None:
    """The critical-bug regression test: a freshly-created Slice-1 project
    has never run (status "idle"), so its FIRST "start building" must be a
    genuine fresh start (resume=False, continue_=False) — NOT continue_=True,
    which 409s "run is not continuable" on an idle project."""
    store.bind_channel("C1", "proj-a")
    calls: list[tuple[str, bool, bool]] = []

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        calls.append((project_id, resume, continue_))
        return {"started": True}

    deps = _deps(tmp_path, start_run_fn=start_run_fn)
    # default fake ledger run_state status is "idle" — no setup needed.

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "started"
    assert calls == [("proj-a", False, False)]


def test_start_run_stopped_project_continues(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    calls: list[tuple[str, bool, bool]] = []

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        calls.append((project_id, resume, continue_))
        return {"started": True}

    deps = _deps(tmp_path, start_run_fn=start_run_fn)
    deps.ledger_factory("proj-a").run_state["status"] = "stopped"

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "started"
    assert calls == [("proj-a", False, True)]


def test_start_run_interrupted_project_resumes(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    calls: list[tuple[str, bool, bool]] = []

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        calls.append((project_id, resume, continue_))
        return {"started": True}

    deps = _deps(tmp_path, start_run_fn=start_run_fn)
    deps.ledger_factory("proj-a").run_state["status"] = "interrupted"

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "started"
    assert calls == [("proj-a", True, False)]


def test_start_run_already_running_short_circuits_without_calling_start_run_fn(
    tmp_path: Path,
) -> None:
    store.bind_channel("C1", "proj-a")
    calls: list[tuple[str, bool, bool]] = []

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        calls.append((project_id, resume, continue_))
        return {"started": True}

    deps = _deps(tmp_path, start_run_fn=start_run_fn)
    deps.ledger_factory("proj-a").run_state["status"] = "running"

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "already_running"
    assert calls == []


# --- start_run: 409-shaped "already in progress" is swallowed --------------


def test_start_run_409_already_in_progress_is_swallowed(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")

    class _AlreadyRunning(Exception):
        status_code = 409
        detail = "a run is already in progress"

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        raise _AlreadyRunning()

    deps = _deps(tmp_path, start_run_fn=start_run_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "already_running"


# --- start_run: 409 "run is not continuable" is NOT swallowed as already_running


def test_start_run_409_not_continuable_is_a_clean_error_not_already_running(
    tmp_path: Path,
) -> None:
    """The previously-swallowed shape: a 409 whose detail does NOT contain
    "already in progress" (e.g. a stale/wrong mode was picked) must surface
    as a clean error, not falsely claim the team is already running."""
    store.bind_channel("C1", "proj-a")

    class _NotContinuable(Exception):
        status_code = 409
        detail = "run is not continuable"

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        raise _NotContinuable()

    deps = _deps(tmp_path, start_run_fn=start_run_fn)
    deps.ledger_factory("proj-a").run_state["status"] = "stopped"

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert result["status"] != "already_running"


# --- start_run: run_setup_required 409 (fresh start, team not confirmed) ---


def test_start_run_run_setup_required_409_is_a_clean_error(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")

    class _SetupRequired(Exception):
        status_code = 409
        detail = {
            "code": "run_setup_required",
            "message": "Run setup hasn't been confirmed for this project.",
        }

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        raise _SetupRequired()

    deps = _deps(tmp_path, start_run_fn=start_run_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "configured" in result["detail"]


# --- start_run: member-health preflight 409 is a real "can't start" --------
#
# Only reachable on a FRESH start — resume/continue skip the preflight
# (routes/coding.py:2586) — so this test deliberately leaves run_state at
# its default "idle" (fresh start is what gets attempted).


def test_start_run_member_health_preflight_409_surfaces_reason(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")

    class _Preflight(Exception):
        status_code = 409
        detail = {
            "code": "member_health_preflight_failed",
            "message": "provider 'claude-cli' is logged out",
        }

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        raise _Preflight()

    deps = _deps(tmp_path, start_run_fn=start_run_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "logged out" in result["detail"]


# --- start_run: arbitrary exception -> clean error, no crash ----------------


def test_start_run_arbitrary_exception_is_clean_error_not_a_crash(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")

    def start_run_fn(
        project_id: str, *, resume: bool = False, continue_: bool = False,
    ) -> dict[str, Any]:
        raise RuntimeError("boom, secret token leaked here")

    deps = _deps(tmp_path, start_run_fn=start_run_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "RuntimeError" in result["detail"]
    assert "boom" not in result["detail"]
    assert "secret token" not in result["detail"]


# --- start_run: default seam is lazily imported, never at module load ------


def test_default_start_run_lazily_imports_and_calls_start_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errorta_app.routes.coding as coding_routes

    calls: list[tuple[str, dict[str, Any], bool, bool]] = []

    def fake_start_run(project_id: str, body: dict[str, Any], *, continue_: bool = False,
                        resume: bool = False) -> dict[str, Any]:
        calls.append((project_id, body, resume, continue_))
        return {"started": True}

    monkeypatch.setattr(coding_routes, "_start_run", fake_start_run)

    result = tools._default_start_run("proj-z", resume=False, continue_=True)

    assert result == {"started": True}
    assert calls == [("proj-z", {}, False, True)]


# --- stop_run ----------------------------------------------------------------


def test_stop_run_when_running_sets_cancel_requested(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger_store = deps.ledger_factory("proj-a")
    ledger_store.run_state["status"] = "running"

    result = tools.dispatch(
        "stop_run", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "stopping"
    assert ledger_store.run_state["cancel_requested"] is True


def test_stop_run_when_idle_is_a_friendly_no_op(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger_store = deps.ledger_factory("proj-a")

    result = tools.dispatch(
        "stop_run", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "not_running"
    assert ledger_store.run_state["cancel_requested"] is False


def test_stop_run_is_r_class_and_does_not_stage(tmp_path: Path) -> None:
    """stop_run is R-class: even with confirmed_via=None it executes
    directly (no staged confirmation)."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger_store = deps.ledger_factory("proj-a")
    ledger_store.run_state["status"] = "running"

    result = tools.dispatch(
        "stop_run", {}, channel_id="C1", thread_ts="t1", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "stopping"
    assert result.get("status") != "needs_confirmation"


# --- project_status gains run_status ----------------------------------------


def test_project_status_includes_run_status(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger_store = deps.ledger_factory("proj-a")
    ledger_store.run_state["status"] = "running"

    result = tools.dispatch(
        "project_status", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["run_status"] == "running"
    # existing keys are preserved
    assert "tasks" in result
    assert "blockers" in result


def test_project_status_run_status_defaults_to_idle(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "project_status", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["run_status"] == "idle"


# --- reconfigure_team ---------------------------------------------------
#
# Uses a REAL ``LedgerStore`` (not the fake used elsewhere in this file) so
# ``control_actions.assign_models_by_role`` mutates and reads back a genuine
# ``run_config`` — mirroring ``tests/coding/test_f145_control_actions.py``'s
# ``_team_project``. ``available_routes`` is always injected so no test ever
# probes the real gateway/Ollama.

_AVAIL = [
    {"route_id": "claude_cli.opus", "family": "opus", "provider_class": "claude_cli"},
    {"route_id": "cursor_cli.composer-2.5", "family": "composer", "provider_class": "cursor_cli"},
]


def _team_project(project_id: str) -> LedgerStore:
    real_store = LedgerStore(project_id)
    real_store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None,
    )
    CodingWorkspace(project_id, real_store).setup(target="new", repo_path=None)
    real_store.set_run_config(room_id=None, members=[
        {"id": "pm-1", "metadata": {"coding_role": "pm"}, "model_mode": "single",
         "gateway_route_id": "cursor_cli.composer-2.5"},
        {"id": "dev-1", "metadata": {"coding_role": "dev"}, "model_mode": "single",
         "gateway_route_id": "cursor_cli.composer-2.5"},
        {"id": "rev-1", "metadata": {"coding_role": "reviewer"}, "model_mode": "single",
         "gateway_route_id": "cursor_cli.composer-2.5"},
    ])
    return real_store


def test_reconfigure_team_trust_is_r() -> None:
    assert tools.TOOL_CATALOG["reconfigure_team"]["trust"] == "R"


def test_tool_deps_available_routes_defaults_to_none() -> None:
    assert tools.ToolDeps().available_routes is None


def test_reconfigure_team_empty_role_routes_is_a_clean_error_no_engine_call(
    tmp_path: Path,
) -> None:
    # Deliberately an UNBOUND channel: if the empty-args check happened
    # after resolving the project, this would raise `no_project_bound`
    # instead of the friendly message — proving the empty check short-
    # circuits before any engine/project lookup happens.
    deps = _deps(tmp_path, available_routes=list(_AVAIL))

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {}},
        channel_id="C-unbound", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "role" in result["detail"].lower()
    assert "model" in result["detail"].lower()


def test_reconfigure_team_missing_role_routes_key_is_the_same_clean_error(
    tmp_path: Path,
) -> None:
    deps = _deps(tmp_path, available_routes=list(_AVAIL))

    result = tools.dispatch(
        "reconfigure_team", {}, channel_id="C-unbound", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "role" in result["detail"].lower()


def test_reconfigure_team_valid_role_route_changes_member_and_returns_reconfigured(
    tmp_path: Path,
) -> None:
    project_id = "proj-reconf-ok"
    real_store = _team_project(project_id)
    store.bind_channel("C1", project_id)
    deps = _deps(tmp_path, ledger_factory=LedgerStore, available_routes=list(_AVAIL))

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {"reviewer": "opus"}},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result == {"status": "reconfigured", "changes": {"reviewer": "opus"}}
    cfg = real_store.get_run_config()
    rev = next(m for m in cfg["members"] if m["metadata"]["coding_role"] == "reviewer")
    assert rev["gateway_route_id"] == "claude_cli.opus"
    # the dev role, not named in role_routes, is untouched
    dev = next(m for m in cfg["members"] if m["metadata"]["coding_role"] == "dev")
    assert dev["gateway_route_id"] == "cursor_cli.composer-2.5"


def test_reconfigure_team_unavailable_model_is_clean_error_with_candidates_no_mutation(
    tmp_path: Path,
) -> None:
    project_id = "proj-reconf-bad-model"
    real_store = _team_project(project_id)
    store.bind_channel("C1", project_id)
    deps = _deps(tmp_path, ledger_factory=LedgerStore, available_routes=list(_AVAIL))

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {"reviewer": "gpt5"}},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "gpt5" in result["detail"]
    # the candidates (available models) are surfaced, not just "no match"
    assert "claude_cli.opus" in result["detail"]
    assert "cursor_cli.composer-2.5" in result["detail"]
    # the team was NOT mutated
    cfg = real_store.get_run_config()
    rev = next(m for m in cfg["members"] if m["metadata"]["coding_role"] == "reviewer")
    assert rev["gateway_route_id"] == "cursor_cli.composer-2.5"


def test_reconfigure_team_role_with_no_member_is_a_clean_error(tmp_path: Path) -> None:
    project_id = "proj-reconf-no-role"
    _team_project(project_id)  # no "tester" role on this team
    store.bind_channel("C1", project_id)
    deps = _deps(tmp_path, ledger_factory=LedgerStore, available_routes=list(_AVAIL))

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {"tester": "opus"}},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "matched" in result["detail"] or "no_matching_members" in result["detail"]


def test_reconfigure_team_arbitrary_exception_is_clean_error_not_a_crash(
    tmp_path: Path,
) -> None:
    store.bind_channel("C1", "proj-reconf-boom")

    class _BoomStore:
        def get_run_config(self) -> dict[str, Any]:
            raise RuntimeError("boom, secret token leaked here")

    deps = _deps(
        tmp_path, ledger_factory=lambda project_id: _BoomStore(),
        available_routes=list(_AVAIL),
    )

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {"reviewer": "opus"}},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "error"
    assert "RuntimeError" in result["detail"]
    assert "secret token" not in result["detail"]


def test_reconfigure_team_calls_list_available_routes_when_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from errorta_council.coding import pm_reference

    project_id = "proj-reconf-lazy"
    _team_project(project_id)
    store.bind_channel("C1", project_id)
    calls: list[int] = []

    def fake_list_available_routes() -> list[dict[str, Any]]:
        calls.append(1)
        return list(_AVAIL)

    monkeypatch.setattr(pm_reference, "list_available_routes", fake_list_available_routes)
    deps = _deps(tmp_path, ledger_factory=LedgerStore)  # available_routes left at default None

    result = tools.dispatch(
        "reconfigure_team", {"role_routes": {"reviewer": "opus"}},
        channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result["status"] == "reconfigured"
    assert len(calls) == 1


def test_default_start_run_fresh_recovers_saved_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh 'start building' on a brand-new studio project must pass the
    project's already-confirmed team to _start_run — otherwise _start_run
    (which only recovers the saved team on resume/continue) fails with
    'no members' and the run never starts."""
    import errorta_app.routes.coding as coding_routes
    import errorta_council.coding.ledger as ledger_mod

    captured: dict[str, Any] = {}

    def fake_start_run(pid, body, *, resume=False, continue_=False):
        captured.update(pid=pid, body=body, resume=resume, continue_=continue_)
        return {"status": "started"}

    class FakeLS:
        def __init__(self, pid): pass
        def get_run_config(self):
            return {"members": [{"id": "pm-1", "gateway_route_id": "claude_cli.opus"},
                                {"id": "designer-1", "gateway_route_id": "claude_cli.opus"}]}

    monkeypatch.setattr(coding_routes, "_start_run", fake_start_run)
    monkeypatch.setattr(ledger_mod, "LedgerStore", FakeLS)

    tools._default_start_run("p1", resume=False, continue_=False)

    assert captured["continue_"] is False and captured["resume"] is False
    assert captured["body"] == {"members": [
        {"id": "pm-1", "gateway_route_id": "claude_cli.opus"},
        {"id": "designer-1", "gateway_route_id": "claude_cli.opus"},
    ]}


def test_default_start_run_resume_does_not_recover_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume/continue must still pass an EMPTY body — _start_run recovers the
    saved team itself for those modes, so the fix must not double-inject."""
    import errorta_app.routes.coding as coding_routes

    captured: dict[str, Any] = {}
    monkeypatch.setattr(coding_routes, "_start_run",
                        lambda pid, body, *, resume=False, continue_=False: captured.update(body=body) or {"status": "started"})

    tools._default_start_run("p1", resume=True, continue_=False)
    assert captured["body"] == {}


# --- set_next_goal: the anti-inert proof + injection wall -------------------


def test_set_next_goal_reaches_the_run_loops_pm_prompt(tmp_path: Path) -> None:
    """THE anti-inert test (spec §2.1, §4). runner._pm_prompt scopes the
    team's planning by store.active_focuses() and explicitly demotes the
    north star to "REFERENCE ONLY — not a list of things to build now"
    (runner.py:3160-3167). So a goal set from Slack is only real if it lands
    in a Focus row that _pm_prompt renders.

    Asserting add_focus was called would NOT prove that. This builds the
    actual PM prompt from a real ledger and greps it."""
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.runner import _pm_prompt

    ledger = LedgerStore("proj-inert")
    ledger.create_project(
        north_star="Stale north star nobody should plan from.",
        definition_of_done="Whatever.",
        target="new", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-inert")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "set_next_goal",
        {"title": "Route mind writes through the reducer", "body": "P2a task 4b"},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "goal_set"
    assert result["focus_id"]

    prompt = _pm_prompt(LedgerStore("proj-inert"))
    assert "Route mind writes through the reducer" in prompt
    assert "P2a task 4b" in prompt
    assert "CURRENT FOCUS" in prompt


def test_set_next_goal_from_chat_text_only_stages(tmp_path: Path) -> None:
    """C-class injection wall: pasted Slack text must never write a goal the
    team then executes. Only a verified block_actions click may."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "set_next_goal", {"title": "Injected goal"},
        channel_id="C1", thread_ts="1.0", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert result["confirmation_id"]
    ledger = deps._ledger_stores.get("proj-a")
    assert ledger is None or not getattr(ledger, "added_focuses", [])


def test_set_next_goal_rejects_an_empty_title(tmp_path: Path) -> None:
    """add_focus raises LedgerError on an empty title (ledger.py:1721) —
    that must become a clean result, never an uncaught raise in a live turn."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-empty")
    ledger.create_project(
        north_star="n", definition_of_done="d",
        target="new", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-empty")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "set_next_goal", {"title": "   "},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "title" in result["detail"].lower()


# --- start_run: shared start_gate refuses a run with no operative goal -----


def test_start_run_refuses_when_the_project_has_no_goal(tmp_path: Path) -> None:
    """Spec §3.4: adopting abovo and pressing start today would launch a run
    whose PM plans against a ten-day-stale north star. Refuse, name the
    remedy, and — critically — do NOT call start_run_fn."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-nogoal")
    ledger.create_project(
        north_star="Stale.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-nogoal")
    calls: list[tuple[str, bool, bool]] = []

    def _start_fn(pid: str, *, resume: bool = False, continue_: bool = False) -> dict:
        calls.append((pid, resume, continue_))
        return {"status": "started"}

    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
                 start_run_fn=_start_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "refused"
    assert "goal" in result["detail"].lower()
    assert calls == []


def test_start_run_proceeds_once_a_goal_is_set(tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-goal")
    ledger.create_project(
        north_star="Stale.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.add_focus(title="Build the tick engine", origin="slack_pm")
    store.bind_channel("C1", "proj-goal")
    calls: list[tuple[str, bool, bool]] = []

    def _start_fn(pid: str, *, resume: bool = False, continue_: bool = False) -> dict:
        calls.append((pid, resume, continue_))
        return {"status": "started"}

    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
                 start_run_fn=_start_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "started"
    assert calls == [("proj-goal", False, False)]


@pytest.mark.parametrize(
    ("run_status", "expected_mode"),
    [("interrupted", (True, False)), ("stopped", (False, True))],
)
def test_start_run_does_not_gate_a_resume_or_continue(
    tmp_path: Path, run_status: str, expected_mode: tuple[bool, bool],
) -> None:
    """Regression (branch review #13): the gate was evaluated before the mode
    was derived, so it applied to all three. A project whose only Focus was
    archived on completion and whose run was then INTERRUPTED mid-task could
    not be resumed from Slack at all — the PM had to invent a new goal first,
    which changes what the resumed run plans against. The gate's own rationale
    ("the PM would plan from the North Star alone") is a fresh-planning
    argument; a resume is not fresh planning."""
    from errorta_council.coding.ledger import LedgerStore

    project_id = f"proj-{run_status}"
    ledger = LedgerStore(project_id)
    ledger.create_project(
        north_star="Stale.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.set_run_state(status=run_status)
    assert ledger.active_focuses() == []  # the gate would refuse a fresh start
    store.bind_channel("C1", project_id)
    calls: list[tuple[str, bool, bool]] = []

    def _start_fn(pid: str, *, resume: bool = False, continue_: bool = False) -> dict:
        calls.append((pid, resume, continue_))
        return {"status": "started"}

    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
                 start_run_fn=_start_fn)

    result = tools.dispatch(
        "start_run", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "started"
    assert calls == [(project_id, *expected_mode)]


# --- set_north_star: writes through the lock-held authoritative writer -----


def test_set_north_star_writes_through_promote_north_star(tmp_path: Path) -> None:
    """Must use LedgerStore.promote_north_star (ledger.py:1878) — the only
    lock-held writer, which bumps revision. NOT the PUT /north-star route's
    unlocked read-modify-write, which can lose-update against a concurrent
    run write (see spec §2.2)."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-ns")
    ledger.create_project(
        north_star="Old star.", definition_of_done="Old done.",
        target="existing", repo_path=None, delivery_root=None,
    )
    before = ledger.get_project().revision
    store.bind_channel("C1", "proj-ns")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "set_north_star",
        {"north_star": "New star.", "definition_of_done": "New done."},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "north_star_set"
    project = LedgerStore("proj-ns").get_project()
    assert project.north_star == "New star."
    assert project.definition_of_done == "New done."
    assert project.revision == before + 1


def test_set_north_star_does_not_record_a_new_purpose_as_already_met(
    tmp_path: Path,
) -> None:
    """Regression (branch review #8): `promote_north_star` forward-stamps
    `north_star_met_at` for `target == "existing"`, and `Project.phase` returns
    "steering" whenever that field is set. The stamp exists for the F141 import
    flow, where the North Star was INFERRED FROM the existing codebase and is
    therefore already true. A human naming a NEW purpose from Slack is the
    opposite case: "our north star is now: ship a multiplayer mode" on an
    adopted project was instantly recorded as met, with zero code behind it,
    and the project flipped to the steering phase."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-ns-unmet")
    ledger.create_project(
        north_star="Inferred from the existing code.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    assert ledger.get_project().north_star_met_at == ""
    store.bind_channel("C1", "proj-ns-unmet")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "set_north_star", {"north_star": "Ship a multiplayer mode."},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "north_star_set"
    project = LedgerStore("proj-ns-unmet").get_project()
    assert project.north_star == "Ship a multiplayer mode."
    assert project.north_star_met_at == "", "an unbuilt north star was recorded as met"
    assert project.phase == "north_star"


def test_set_north_star_leaves_an_already_steering_project_in_steering(
    tmp_path: Path,
) -> None:
    """The stamp is forward-only. Declining to ADD one must not remove one: a
    project that genuinely crossed into steering stays there when its North
    Star is later replaced."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-ns-steering")
    ledger.create_project(
        north_star="Old.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.mark_north_star_met()
    store.bind_channel("C1", "proj-ns-steering")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    tools.dispatch(
        "set_north_star", {"north_star": "Next chapter."},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert LedgerStore("proj-ns-steering").get_project().phase == "steering"


def test_set_north_star_preserves_dod_when_omitted(tmp_path: Path) -> None:
    """The in-app modal sends only northStar and never definitionOfDone
    (src/features/coding/index.tsx:1177-1183). An omitted DoD must leave the
    stored one intact, not blank it."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-dod")
    ledger.create_project(
        north_star="Old star.", definition_of_done="Keep me.",
        target="existing", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-dod")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    tools.dispatch(
        "set_north_star", {"north_star": "New star."},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert LedgerStore("proj-dod").get_project().definition_of_done == "Keep me."


def test_set_north_star_refuses_mid_run(tmp_path: Path) -> None:
    """Mirrors accept_north_star_proposal's 409 guard
    (routes/coding.py:4598-4599): rewriting the charter under a live run
    changes what the team is building mid-flight."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-live")
    ledger.create_project(
        north_star="Old star.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.set_run_state(status="running")
    store.bind_channel("C1", "proj-live")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "set_north_star", {"north_star": "New star."},
        channel_id="C1", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "run" in result["detail"].lower()
    assert LedgerStore("proj-live").get_project().north_star == "Old star."


def test_set_north_star_from_chat_text_only_stages(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)

    result = tools.dispatch(
        "set_north_star", {"north_star": "Injected star."},
        channel_id="C1", thread_ts="1.0", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"


def test_propose_next_goal_returns_a_proposal_and_writes_nothing(tmp_path: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-propose")
    ledger.create_project(
        north_star="Stale.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.set_run_config(members=[dict(_PM_MEMBER)])
    store.bind_channel("C1", "proj-propose")
    deps = _deps(
        tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
        propose_goal_fn=lambda store_, **kw: {
            "title": "Route mind writes through the reducer",
            "body": "P2a task 4b", "evidence": ["abovo/mind.py"], "stale": True},
        goal_caller=lambda member, prompt: "{}",
    )

    result = tools.dispatch(
        "propose_next_goal", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via=None, deps=deps,
    )

    # R-class: runs immediately from chat text, no confirmation needed.
    assert result["status"] == "proposed"
    assert result["title"] == "Route mind writes through the reducer"
    assert result["stale"] is True
    assert LedgerStore("proj-propose").active_focuses() == []


def test_propose_next_goal_reports_a_thin_repo_instead_of_inventing(tmp_path: Path) -> None:
    """An empty title means the read was too thin to ground a goal. The verb
    must say so rather than pass an empty goal along as if it were real."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-thin")
    ledger.create_project(
        north_star="Stale.", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.set_run_config(members=[dict(_PM_MEMBER)])
    store.bind_channel("C1", "proj-thin")
    deps = _deps(
        tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
        propose_goal_fn=lambda store_, **kw: {
            "title": "", "body": "", "evidence": [], "stale": False},
        goal_caller=lambda member, prompt: "{}",
    )

    result = tools.dispatch(
        "propose_next_goal", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "no_proposal"


def test_tool_deps_propose_goal_fn_defaults_to_none(tmp_path: Path) -> None:
    """Must default to None, not the real helper — the real one imports
    repo_reader and shells out to git, neither of which may happen at
    ToolDeps() construction time."""
    assert tools.ToolDeps().propose_goal_fn is None


def test_propose_next_goal_refuses_without_a_model_caller(tmp_path: Path) -> None:
    """goal_caller defaults to None (no model wired up). The verb must refuse
    cleanly rather than call None and raise inside a live Slack turn."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-nocaller")
    ledger.create_project(
        north_star="n", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-nocaller")
    deps = _deps(tmp_path, ledger_factory=lambda pid: LedgerStore(pid))

    result = tools.dispatch(
        "propose_next_goal", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "error"
    assert "model" in result["detail"].lower()


def test_propose_next_goal_ignores_a_model_supplied_gateway_route(tmp_path: Path) -> None:
    """Regression (branch review #5): the member was taken from ``args``, which
    is whatever the concierge model emitted — i.e. ultimately derived from
    chat text, including anything a user pasted. ``propose_next_goal`` is
    R-class, so it runs with no confirmation at all; honouring that member let
    pasted text pick a paid cloud gateway route and make a billed call, which
    is precisely the decision the C-class ``spend_cloud`` verb exists to gate.

    The route must come from the project's persisted run config only."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-route")
    ledger.create_project(
        north_star="n", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    ledger.set_run_config(members=[dict(_PM_MEMBER)])
    store.bind_channel("C1", "proj-route")

    seen: dict[str, Any] = {}

    def spy_propose(store_: Any, *, member: dict[str, Any], caller: Any) -> dict[str, Any]:
        seen["member"] = member
        return {"title": "t", "body": "b", "evidence": [], "stale": False}

    deps = _deps(
        tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
        propose_goal_fn=spy_propose,
        goal_caller=lambda member, prompt: "{}",
    )

    result = tools.dispatch(
        "propose_next_goal",
        {"member": {"gateway_route_id": "cloud.expensive-frontier",
                    "provider_kind": "http",
                    "turn_limits": {"timeout_seconds": 600}}},
        channel_id="C1", thread_ts="1.0", confirmed_via=None, deps=deps,
    )

    assert result["status"] == "proposed"
    assert seen["member"]["gateway_route_id"] == "local.pm-route"
    assert "cloud.expensive-frontier" not in str(seen["member"])
    assert "turn_limits" not in seen["member"]


def test_propose_next_goal_refuses_when_the_team_has_no_pm_route(tmp_path: Path) -> None:
    """No PM in the run config means there is no trusted route to call. Refuse
    rather than fall back to an empty ``gateway_route_id``, which would crash
    inside the gateway mid-turn."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-nopm")
    ledger.create_project(
        north_star="n", definition_of_done="d",
        target="existing", repo_path=None, delivery_root=None,
    )
    store.bind_channel("C1", "proj-nopm")
    propose_calls: list[dict[str, Any]] = []
    deps = _deps(
        tmp_path, ledger_factory=lambda pid: LedgerStore(pid),
        propose_goal_fn=lambda store_, **kw: (
            propose_calls.append(kw) or {"title": "t", "body": "", "evidence": []}),
        goal_caller=lambda member, prompt: "{}",
    )

    result = tools.dispatch(
        "propose_next_goal", {}, channel_id="C1", thread_ts="1.0",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "error"
    assert propose_calls == [], "reached the model with no configured route"


# --------------------------------------------------------------------------
# Slice 5b Task 5 — the set_updates verb.
# --------------------------------------------------------------------------


def test_set_updates_off_then_on(tmp_errorta_home) -> None:
    from errorta_slack import store as real_store

    deps = tools.ToolDeps(store=real_store)

    off = tools.dispatch("set_updates", {"on": False}, channel_id="C1",
                         thread_ts="t1", confirmed_via=None, deps=deps)
    assert off == {"status": "updated", "updates": "off"}
    assert real_store.updates_muted("C1") is True

    on = tools.dispatch("set_updates", {"on": True}, channel_id="C1",
                        thread_ts="t1", confirmed_via=None, deps=deps)
    assert on == {"status": "updated", "updates": "on"}
    assert real_store.updates_muted("C1") is False


def test_set_updates_defaults_to_on_when_the_arg_is_missing(tmp_errorta_home) -> None:
    """The mute is the surprising state; an ambiguous request must not land
    there. A model that omits the arg turns updates ON, never off."""
    from errorta_slack import store as real_store

    deps = tools.ToolDeps(store=real_store)
    real_store.set_updates("C2", enabled=False)

    result = tools.dispatch("set_updates", {}, channel_id="C2", thread_ts="t1",
                            confirmed_via=None, deps=deps)

    assert result["updates"] == "on"
    assert real_store.updates_muted("C2") is False


def test_set_updates_is_an_R_verb_and_needs_no_confirmation() -> None:
    """[R]: it spends nothing and is reversible from the same chat."""
    assert tools.TOOL_CATALOG["set_updates"]["trust"] == "R"


# --------------------------------------------------------------------------
# Slice 6 — closing the control loop from the channel.
#
# A completion_blocked run told the operator to "finish or cancel these tasks"
# and no Slack verb could do either. Designing the fix surfaced a second
# defect: nothing the PM can see carries a task_id, so a cancel verb taking one
# would have shipped uncallable.
# --------------------------------------------------------------------------


def _seed_tasks(deps: tools.ToolDeps, *tasks: FakeTask) -> Any:
    ledger = deps.ledger_factory("proj-a")
    ledger.tasks = list(tasks)
    return ledger


def test_list_open_tasks_returns_id_title_state(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps, FakeTask("t-1", "todo", "open one"),
                FakeTask("t-2", "done", "finished one"))

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert out["tasks"] == [{"task_id": "t-1", "title": "open one", "state": "todo"}]


def test_list_open_tasks_excludes_terminal_states(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps, FakeTask("t-1", "done", "a"), FakeTask("t-2", "dropped", "b"),
                FakeTask("t-3", "merged", "c"), FakeTask("t-4", "blocked", "d"))

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert [t["task_id"] for t in out["tasks"]] == ["t-4"]


def test_list_open_tasks_caps_at_twenty(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps, *[FakeTask(f"t-{i}", "todo", f"task {i}") for i in range(40)])

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert len(out["tasks"]) == 20


def test_cancel_task_drops_it(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger = _seed_tasks(deps, FakeTask("t-1", "todo", "open one"))

    out = tools.dispatch("cancel_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out == {"status": "cancelled", "task_id": "t-1"}
    assert ledger.updates == [("t-1", {"state": "dropped"})]


def test_cancel_task_unknown_id_is_an_error_result_not_a_raise(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps)

    out = tools.dispatch("cancel_task", {"task_id": "nope"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "error"
    assert "unknown task" in out["detail"]


def test_cancel_task_from_chat_text_only_stages(tmp_path: Path) -> None:
    """The injection wall: a [C] verb never executes from chat text alone."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger = _seed_tasks(deps, FakeTask("t-1", "todo", "open one"))

    out = tools.dispatch("cancel_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via=None, deps=deps)

    assert out["status"] == "needs_confirmation"
    assert ledger.updates == []


def test_unblock_task_moves_blocked_to_todo(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger = _seed_tasks(deps, FakeTask("t-1", "blocked", "stuck one"))

    out = tools.dispatch("unblock_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out == {"status": "unblocked", "task_id": "t-1"}
    assert ledger.updates == [("t-1", {"state": "todo"})]


def test_unblock_task_refuses_a_task_that_is_not_blocked(tmp_path: Path) -> None:
    """An unblock, not a general state setter -- it must not rewind done work."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger = _seed_tasks(deps, FakeTask("t-1", "done", "finished"))

    out = tools.dispatch("unblock_task", {"task_id": "t-1"}, channel_id="C1",
                         thread_ts="t", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "error"
    assert ledger.updates == []


def test_list_open_tasks_reports_truncation(tmp_path: Path) -> None:
    """Review fix: a silent cap makes the PM report 20 of 40 as if it were all,
    so the operator clears those 20 and hits the completion gate again on tasks
    they were never shown."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps, *[FakeTask(f"t-{i}", "todo", f"task {i}") for i in range(40)])

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert out["total_open"] == 40
    assert out["truncated"] is True
    assert len(out["tasks"]) == 20


def test_list_open_tasks_not_truncated_when_it_fits(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    _seed_tasks(deps, FakeTask("t-1", "todo", "only one"))

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert out["total_open"] == 1
    assert out["truncated"] is False


def test_task_verbs_never_let_an_engine_failure_escape(tmp_path: Path) -> None:
    """Review fix: catching only LedgerError let an OSError out of the verb and
    into the turn, where nothing catches it -- the operator gets the generic
    "couldn't process that" instead of a reportable result."""
    class _Exploding(FakeTask):
        pass

    store.bind_channel("C1", "proj-a")

    for verb, state in (("cancel_task", "todo"), ("unblock_task", "blocked")):
        deps = _deps(tmp_path)
        ledger = _seed_tasks(deps, _Exploding("t-1", state, "one"))

        def _boom(task_id: str, **patch: Any) -> Any:
            raise OSError("disk full")

        ledger.update_task = _boom

        out = tools.dispatch(verb, {"task_id": "t-1"}, channel_id="C1",
                             thread_ts="t", confirmed_via="block_actions", deps=deps)

        assert out["status"] == "error", verb
        assert "OSError" in out["detail"], verb
        assert "disk full" not in out["detail"], f"{verb} leaked the message"


def test_list_open_tasks_survives_an_unreadable_backlog(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    ledger = deps.ledger_factory("proj-a")

    def _boom() -> Any:
        raise OSError("corrupt backlog")

    ledger.list_tasks = _boom

    out = tools.dispatch("list_open_tasks", {}, channel_id="C1", thread_ts="t",
                         confirmed_via=None, deps=deps)

    assert out["status"] == "error"
    assert "OSError" in out["detail"]


# --------------------------------------------------------------------------
# Live-run supervisor verbs (spec 2026-08-21 §3.7).
#
# Every seam is injected, so none of these touch `errorta_liverun` at all --
# which is also the point: importing `tools` must never import the supervisor.
# --------------------------------------------------------------------------


def _liverun_deps(calls: list) -> tools.ToolDeps:
    store.bind_channel("C9", "proj-lr")
    return tools.ToolDeps(
        liverun_list_fn=lambda: [{"name": "osrs", "valid": True, "error": None}],
        liverun_start_fn=lambda profile, project_id: (
            calls.append(("start", profile, project_id)) or {"status": "started", "run_id": "r1"}),
        liverun_stop_fn=lambda project_id: (
            calls.append(("stop", project_id)) or {"status": "stopping"}),
        liverun_status_fn=lambda project_id: {"status": "live", "phase": "watching", "probes": {}},
        liverun_resume_fn=lambda profile: (
            calls.append(("resume", profile)) or {"status": "resumed"}),
    )


def test_liverun_catalog_trust_classes() -> None:
    assert tools.TOOL_CATALOG["start_live_run"]["trust"] == "C"
    assert tools.TOOL_CATALOG["resume_live_run"]["trust"] == "C"
    for verb in ("stop_live_run", "live_status", "list_live_profiles"):
        assert tools.TOOL_CATALOG[verb]["trust"] == "R"
    assert tools.HUMAN_ONLY_VERBS == frozenset({"resume_live_run", "resume_fix_loop"})


def test_start_live_run_stages_then_fires_with_block_actions() -> None:
    calls: list = []
    deps = _liverun_deps(calls)

    staged = tools.dispatch("start_live_run", {"profile": "osrs"},
                            channel_id="C9", thread_ts="1", deps=deps)

    assert staged["status"] == "needs_confirmation" and calls == []

    fired = tools.dispatch("start_live_run", {"profile": "osrs"}, channel_id="C9",
                           thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert fired == {"status": "started", "run_id": "r1"}
    assert calls == [("start", "osrs", "proj-lr")]


@pytest.mark.parametrize("args", [{}, {"profile": "../x"}, {"profile": ".hidden"},
                                  {"profile": "a" * 65}, {"profile": "has space"}])
def test_start_live_run_rejects_a_bad_profile_name(args: dict[str, Any]) -> None:
    deps = _liverun_deps([])

    with pytest.raises(tools.ToolError) as excinfo:
        tools.dispatch("start_live_run", args, channel_id="C9", thread_ts="1",
                       confirmed_via="block_actions", deps=deps)

    assert excinfo.value.code == "bad_profile_name"


def test_stop_live_run_is_R_and_immediate() -> None:
    calls: list = []
    deps = _liverun_deps(calls)

    out = tools.dispatch("stop_live_run", {}, channel_id="C9", thread_ts="1", deps=deps)

    assert out == {"status": "stopping"} and calls == [("stop", "proj-lr")]


def test_live_status_and_list_live_profiles() -> None:
    deps = _liverun_deps([])

    status = tools.dispatch("live_status", {}, channel_id="C9", thread_ts="1", deps=deps)
    listing = tools.dispatch("list_live_profiles", {}, channel_id="C9", thread_ts="1", deps=deps)

    assert status["phase"] == "watching"
    assert listing == {"status": "ok",
                       "profiles": [{"name": "osrs", "valid": True, "error": None}]}


def test_resume_live_run_is_C() -> None:
    calls: list = []
    deps = _liverun_deps(calls)

    staged = tools.dispatch("resume_live_run", {"profile": "osrs"},
                            channel_id="C9", thread_ts="1", deps=deps)

    assert staged["status"] == "needs_confirmation" and calls == []
    assert tools.dispatch("resume_live_run", {"profile": "osrs"}, channel_id="C9",
                          thread_ts="1", confirmed_via="block_actions",
                          deps=deps) == {"status": "resumed"}
    assert calls == [("resume", "osrs")]


def test_liverun_verbs_need_a_bound_project() -> None:
    deps = _liverun_deps([])

    for verb in ("start_live_run", "stop_live_run", "live_status", "resume_live_run"):
        with pytest.raises(tools.ToolError) as excinfo:
            tools.dispatch(verb, {"profile": "osrs"}, channel_id="C-unbound",
                           thread_ts="1", confirmed_via="block_actions", deps=deps)
        assert excinfo.value.code == "no_project_bound", verb


def test_tools_does_not_import_the_live_run_supervisor_at_module_load() -> None:
    """The seams default to `None` and resolve lazily precisely so that
    `errorta_slack.tools` -- imported on every bridge start -- never drags in
    `errorta_liverun`, which owns launch/subprocess machinery.

    Order-independent by construction (mirrors `test_outbound_module_does_not_
    import_slack_sdk`): reads this module's OWN bound names and source text,
    never process-global `sys.modules`, so a sibling test importing the
    supervisor earlier in the same session cannot false-fail it.
    """
    import inspect

    assert "errorta_liverun" not in vars(tools)
    for line in inspect.getsource(tools).splitlines():
        if "errorta_liverun" in line and "import " in line:
            assert line.startswith(" "), f"module-level supervisor import: {line!r}"
    # And the seams themselves must not be pre-bound to real callables.
    fresh = tools.ToolDeps()
    assert [fresh.liverun_list_fn, fresh.liverun_start_fn, fresh.liverun_stop_fn,
            fresh.liverun_status_fn, fresh.liverun_resume_fn] == [None] * 5


# --------------------------------------------------------------------------
# Live-run FIX loop (spec 2026-08-22 §3.7): `accept_live_fix`, the args-aware
# human-only predicate, and the fix-loop pause/resume pair.
#
# `accept_live_fix` is the ONE verb an autonomous loop can use to put code in
# the operator's real files. It exists as a SEPARATE verb from the desktop
# app's `accept_worktree` route for exactly one reason: that route takes an
# `override` that skips the merge gate, and a loop must never hold that
# switch. There is a grep test below; treat it as load-bearing.
# --------------------------------------------------------------------------


class _FakeWorkspace:
    def __init__(self, delivers: "list[str] | None" = None,
                 preview_error: BaseException | None = None) -> None:
        self.accept_calls: list[dict[str, Any]] = []
        self.head_value = "abc1234"
        # What `merge_back` would actually write into the operator's tree —
        # the workspace's own answer, which is what the effect must read.
        self.delivers = ["app.py"] if delivers is None else list(delivers)
        self.preview_error = preview_error

    def head(self) -> str:
        return self.head_value

    def preview(self) -> dict[str, Any]:
        if self.preview_error is not None:
            raise self.preview_error
        return {"has_changes": bool(self.delivers), "conflicts": [],
                "changed_files": [{"path": p, "status": "modified"}
                                  for p in self.delivers]}

    def accept(self, **kw: Any) -> dict[str, Any]:
        self.accept_calls.append(kw)
        return {"merged": True, "head": self.head_value}


class _FixLedger(FakeLedgerStore):
    """The ledger surface `accept_live_fix` touches: a project row and the
    decision log it writes its outcome into."""

    def __init__(self, project_id: str, tmp_path: Path) -> None:
        super().__init__(project_id, tmp_path)
        self.decisions: list[dict[str, Any]] = []

    def get_project(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(target="existing", repo_path="/repo", delivery_root=None)

    def record_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)


def _fix_deps(tmp_path: Path, *, gate_allowed: bool = True,
              ws: "_FakeWorkspace | None" = None,
              deliver_calls: list | None = None,
              ledger: "_FixLedger | None" = None) -> tools.ToolDeps:
    store.bind_channel("C1", "p")
    workspace = ws if ws is not None else _FakeWorkspace()
    led = ledger if ledger is not None else _FixLedger("p", tmp_path)
    gate = type("_Gate", (), {"allowed": gate_allowed,
                              "blockers": ()})()
    return _deps(
        tmp_path,
        ledger_factory=lambda project_id: led,
        workspace_factory=lambda project_id: workspace,
        merge_review_fn=lambda s, w: {"_gate": gate, "gate": {"allowed": gate_allowed}},
        deliver_fn=lambda project_id, w, **kw: (
            (deliver_calls if deliver_calls is not None else []).append(
                {"project_id": project_id, **kw})
            or {"delivered_to": "/repo", "open_url": "", "run_hint": ""}),
    )


def _staged_fix_args(**over: Any) -> dict[str, Any]:
    args = {"project_id": "p", "repo_id": "brain", "run_id": "r-1", "task_id": "t-1",
            "cycle": 1, "human_only": False, "changed_paths": ["app.py"]}
    args.update(over)
    return args


def test_accept_live_fix_is_C_and_never_takes_an_override() -> None:
    """The whole reason this verb exists instead of reusing the desktop app's
    `accept_worktree` route: that route takes an `override` that skips the
    merge gate. The word may appear in the docstring EXPLAINING its absence —
    it may not appear in a single line of executable code."""
    import ast
    import inspect

    assert tools.TOOL_CATALOG["accept_live_fix"]["trust"] == "C"
    fn = ast.parse(inspect.getsource(tools.accept_live_fix)).body[0]
    assert isinstance(fn, ast.FunctionDef)
    if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]            # the docstring is prose, not behaviour
    assert "override" not in ast.unparse(fn)
    assert "override" not in {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    # ...and no `override` reaches the real accept from anywhere else either.
    assert "override" not in inspect.getsource(tools._record_fix_accept)


def test_accept_live_fix_refuses_a_blocked_gate_without_merging(tmp_path: Path) -> None:
    ws = _FakeWorkspace()
    delivered: list = []
    deps = _fix_deps(tmp_path, gate_allowed=False, ws=ws, deliver_calls=delivered)

    out = tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                         thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "gate_blocked"
    assert ws.accept_calls == [] and delivered == []


def test_accept_live_fix_accepts_with_confirm_true_then_delivers(tmp_path: Path) -> None:
    ws = _FakeWorkspace()
    delivered: list = []
    deps = _fix_deps(tmp_path, ws=ws, deliver_calls=delivered)

    out = tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                         thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "accepted"
    assert ws.accept_calls == [{"confirm": True}]
    assert delivered[0]["project_id"] == "p"
    assert out["delivered_to"] == "/repo"


def test_accept_live_fix_records_its_outcome_for_the_supervisor(tmp_path: Path) -> None:
    """The supervisor stages the confirmation and then only sees `approved` —
    which says the effect was CLAIMED, not that the merge finished. The
    decision row is the durable statement of what actually happened."""
    ledger = _FixLedger("p", tmp_path)
    deps = _fix_deps(tmp_path, ledger=ledger)

    tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                   thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert [d["choice"] for d in ledger.decisions] == ["accept_live_fix"]
    extra = ledger.decisions[0]["extra"]
    assert extra["status"] == "accepted"
    assert extra["run_id"] == "r-1" and extra["repo_id"] == "brain"


def test_a_blocked_gate_is_recorded_too(tmp_path: Path) -> None:
    ledger = _FixLedger("p", tmp_path)
    deps = _fix_deps(tmp_path, gate_allowed=False, ledger=ledger)

    tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                   thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert ledger.decisions[0]["extra"]["status"] == "gate_blocked"


def test_accept_live_fix_refuses_args_the_supervisor_did_not_stage(tmp_path: Path) -> None:
    """It is in the catalog because `dispatch` can only route catalogued verbs
    — which also means the concierge can compose a call to it. A merge is not
    something a chat turn gets to invent: without the supervisor's run id there
    is no fix cycle this accept belongs to."""
    ws = _FakeWorkspace()
    deps = _fix_deps(tmp_path, ws=ws)

    out = tools.dispatch("accept_live_fix", {"project_id": "p"}, channel_id="C1",
                         thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "refused"
    assert ws.accept_calls == []


# --- the effect re-derives the diff; it does not trust what was staged ----- #
#
# `is_human_only` answers from the confirmation RECORD, and the record is a
# JSON file on disk written minutes earlier. Between staging and firing it can
# be stale (the worktree moved), truncated (`MAX_STAGED_PATHS` caps
# `changed_paths` at 50) or simply wrong. So the last thing before the merge
# asks the workspace itself what it is about to deliver, and refuses when that
# answer is guarded but the record never said so. Refusal is not a dead end:
# the supervisor reads the recorded outcome and pauses the cycle for a human.


def test_a_guarded_delivered_path_refuses_even_when_the_record_says_otherwise(
        tmp_path: Path) -> None:
    ws = _FakeWorkspace(delivers=["app.py", "senditai_ng/safety/killswitch.py"])
    delivered: list = []
    ledger = _FixLedger("p", tmp_path)
    deps = _fix_deps(tmp_path, ws=ws, deliver_calls=delivered, ledger=ledger)

    out = tools.dispatch("accept_live_fix",
                         _staged_fix_args(human_only=False, changed_paths=["app.py"]),
                         channel_id="C1", thread_ts="1",
                         confirmed_via="block_actions", deps=deps)

    assert out["status"] == "refused" and out["reason"] == "guarded_path_mismatch"
    assert ws.accept_calls == [] and delivered == []
    assert ledger.decisions[0]["extra"]["status"] == "refused"


def test_a_guarded_path_the_record_declared_still_merges_on_a_tap(tmp_path: Path) -> None:
    """`human_only: True` means the button was posted for a person, and a
    person is who reached this call. The guard is against a MISMATCH, not
    against guarded diffs — those are exactly what the button is for."""
    ws = _FakeWorkspace(delivers=["errorta_liverun/fixloop.py"])
    deps = _fix_deps(tmp_path, ws=ws)

    out = tools.dispatch("accept_live_fix", _staged_fix_args(human_only=True),
                         channel_id="C1", thread_ts="1",
                         confirmed_via="block_actions", deps=deps)

    assert out["status"] == "accepted"
    assert ws.accept_calls == [{"confirm": True}]


def test_staged_changed_paths_alone_never_decide_the_merge(tmp_path: Path) -> None:
    """The other direction, and the reason this is a re-derivation rather than
    a second read of the same field: the record claims a guarded path, the
    workspace will deliver none, and what the workspace says is what counts."""
    ws = _FakeWorkspace(delivers=["app.py"])
    deps = _fix_deps(tmp_path, ws=ws)

    out = tools.dispatch(
        "accept_live_fix",
        _staged_fix_args(human_only=False,
                         changed_paths=["senditai_ng/safety/killswitch.py"]),
        channel_id="C1", thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "accepted"


def test_a_workspace_that_cannot_say_what_it_delivers_merges_nothing(
        tmp_path: Path) -> None:
    ws = _FakeWorkspace(preview_error=RuntimeError("apply_source_missing"))
    delivered: list = []
    deps = _fix_deps(tmp_path, ws=ws, deliver_calls=delivered)

    out = tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                         thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "refused" and out["reason"] == "diff_unreadable"
    assert ws.accept_calls == [] and delivered == []


def test_the_re_derivation_happens_before_the_gate_is_even_asked(
        tmp_path: Path) -> None:
    """Cheapest strongest guard first: a tampered record must not get as far as
    a merge review against the operator's project."""
    ws = _FakeWorkspace(delivers=["senditai_ng/safety/x.py"])
    reviews: list = []
    deps = _fix_deps(tmp_path, ws=ws)
    real_review = deps.merge_review_fn
    deps = replace(deps, merge_review_fn=lambda s, w: (
        reviews.append(1) or real_review(s, w)))

    out = tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                         thread_ts="1", confirmed_via="block_actions", deps=deps)

    assert out["status"] == "refused"
    assert reviews == []


def test_accept_live_fix_stages_before_it_ever_merges(tmp_path: Path) -> None:
    ws = _FakeWorkspace()
    deps = _fix_deps(tmp_path, ws=ws)

    out = tools.dispatch("accept_live_fix", _staged_fix_args(), channel_id="C1",
                         thread_ts="1", deps=deps)

    assert out["status"] == "needs_confirmation"
    assert ws.accept_calls == []


def test_gate_blocked_is_a_not_done_status() -> None:
    """`connection` and `concierge` both read this set to decide whether a
    reply may claim the action happened. A blocked gate merged nothing."""
    assert "gate_blocked" in tools.NOT_DONE_STATUSES


@pytest.mark.parametrize("verb,args,expected", [
    ("resume_live_run", {}, True),
    ("resume_fix_loop", {}, True),
    ("pause_fix_loop", {}, False),
    ("accept_live_fix", {"human_only": True}, True),
    ("accept_live_fix", {"human_only": False}, False),
    ("accept_live_fix", {}, False),
    ("start_run", {"human_only": True}, False),      # the flag is verb-scoped
])
def test_is_human_only(verb: str, args: dict[str, Any], expected: bool) -> None:
    assert tools.is_human_only(verb, args) is expected


def test_is_human_only_takes_no_args_at_all() -> None:
    assert tools.is_human_only("resume_fix_loop") is True
    assert tools.is_human_only("start_run") is False


@pytest.mark.parametrize("path", ["senditai_ng/safety/limits.py",
                                  "senditai_ng/dispatch/killswitch.py",
                                  "errorta_liverun/supervisor.py",
                                  "../outside.py"])
def test_a_guarded_delivered_path_is_human_only_whatever_the_flag_says(path: str) -> None:
    """The flag is computed by the supervisor; the paths are the evidence. A
    record whose flag was lost (an old staging, a truncated write) must still
    reach a human, so the predicate re-derives the answer from the diff."""
    args = {"human_only": False, "changed_paths": ["app.py", path]}

    assert tools.is_human_only("accept_live_fix", args) is True


def test_an_ordinary_diff_is_not_human_only() -> None:
    args = {"human_only": False, "changed_paths": ["app.py", "src/thing.py"]}

    assert tools.is_human_only("accept_live_fix", args) is False


def test_pause_fix_loop_is_R_and_resume_is_C() -> None:
    assert tools.TOOL_CATALOG["pause_fix_loop"]["trust"] == "R"
    assert tools.TOOL_CATALOG["resume_fix_loop"]["trust"] == "C"
    assert "resume_fix_loop" in tools.HUMAN_ONLY_VERBS


def test_pause_and_resume_fix_loop_reach_the_supervisor(tmp_path: Path) -> None:
    calls: list = []
    store.bind_channel("C1", "p")
    deps = _deps(
        tmp_path,
        liverun_pause_fix_fn=lambda profile: (
            calls.append(("pause", profile)) or {"status": "paused"}),
        liverun_resume_fix_fn=lambda profile: (
            calls.append(("resume", profile)) or {"status": "resumed"}),
    )

    assert tools.dispatch("pause_fix_loop", {"profile": "osrs"}, channel_id="C1",
                          thread_ts="1", deps=deps) == {"status": "paused"}
    assert tools.dispatch("resume_fix_loop", {"profile": "osrs"}, channel_id="C1",
                          thread_ts="1", confirmed_via="block_actions",
                          deps=deps) == {"status": "resumed"}
    assert calls == [("pause", "osrs"), ("resume", "osrs")]


def test_resume_fix_loop_stages_rather_than_firing(tmp_path: Path) -> None:
    calls: list = []
    store.bind_channel("C1", "p")
    deps = _deps(tmp_path, liverun_resume_fix_fn=lambda profile: calls.append(profile))

    out = tools.dispatch("resume_fix_loop", {"profile": "osrs"}, channel_id="C1",
                         thread_ts="1", deps=deps)

    assert out["status"] == "needs_confirmation" and calls == []


@pytest.mark.parametrize("verb", ["pause_fix_loop", "resume_fix_loop"])
def test_fix_loop_verbs_reject_a_bad_profile_name(verb: str, tmp_path: Path) -> None:
    store.bind_channel("C1", "p")
    deps = _deps(tmp_path)

    with pytest.raises(tools.ToolError) as excinfo:
        tools.dispatch(verb, {"profile": "../x"}, channel_id="C1", thread_ts="1",
                       confirmed_via="block_actions", deps=deps)

    assert excinfo.value.code == "bad_profile_name"
