from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from errorta_council.coding.ledger import ProjectNotFound
from errorta_council.coding.runtime_process import RuntimeProcessError, RuntimeProcessManager
from errorta_slack import store, tools


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- Fakes -------------------------------------------------------------


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class FakeLedgerStore:
    """Stub matching the surface build_team_log/attention.list_open touch."""

    def __init__(self, project_id: str, tmp_path: Path) -> None:
        self.project_id = project_id
        self.dir = tmp_path / f"ledger-{project_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.added_tasks: list[dict[str, Any]] = []
        self._next_id = 0

    def list_tasks(self) -> list[Any]:
        return []

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
