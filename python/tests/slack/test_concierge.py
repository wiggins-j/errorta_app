from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errorta_slack import concierge, store, tools


@pytest.fixture(autouse=True)
def _isolated_errorta_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    return tmp_path


# --- Fakes (mirrors test_tools.py's shape) --------------------------------


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class FakeLedgerStore:
    def __init__(self, project_id: str, tmp_path: Path) -> None:
        self.project_id = project_id
        self.dir = tmp_path / f"ledger-{project_id}"
        self.dir.mkdir(parents=True, exist_ok=True)

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
        return FakeTask("t-1")


class _FakePmChanges:
    def accept(self, ledger_store: Any, change_id: str) -> None:
        raise AssertionError("accept must never be called from an unconfirmed concierge turn")

    def decline(self, ledger_store: Any, change_id: str) -> None:
        raise AssertionError("decline must never be called from an unconfirmed concierge turn")


def _deps(tmp_path: Path, **overrides: Any) -> tools.ToolDeps:
    kwargs: dict[str, Any] = {
        "store": store,
        "ledger_factory": lambda project_id: FakeLedgerStore(project_id, tmp_path),
        "launch_fn": lambda project_id: None,
        "publish_fn": lambda args: (_ for _ in ()).throw(
            AssertionError("publish_fn must never run from an unconfirmed concierge turn")
        ),
        "pm_changes_mod": _FakePmChanges(),
    }
    kwargs.update(overrides)
    return tools.ToolDeps(**kwargs)


class _ScriptedCaller:
    """A fake ``caller`` that returns one canned raw-text reply per call, in
    order, and records every ``(member, prompt)`` it was invoked with."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[dict[str, Any], str]] = []

    def __call__(self, member: dict[str, Any], prompt: str) -> str:
        self.calls.append((member, prompt))
        if not self._replies:
            raise AssertionError("_ScriptedCaller called more times than scripted")
        return self._replies.pop(0)


def _envelope(**fields: Any) -> str:
    return json.dumps(fields)


# --- build_system_prompt ----------------------------------------------------


def test_build_system_prompt_includes_catalog_and_etiquette() -> None:
    prompt = concierge.build_system_prompt("proj-a")

    for verb in tools.TOOL_CATALOG:
        assert verb in prompt
    assert "SLACK ETIQUETTE CONTRACT" in prompt
    assert "REPLY FORMAT" in prompt
    assert "confirmation button" in prompt or "Approve" in prompt


# --- Happy path: status query -> dispatch -> reply --------------------------


def test_happy_path_status_query_dispatches_and_replies(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller([
        _envelope(
            reply="Checking on that now...",
            tool_calls=[{"verb": "project_status", "args": {}}],
        ),
        _envelope(reply="Here's the status: no blockers.", tool_calls=[]),
    ])

    result = concierge.run_turn(
        "how's it going", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert result["reply"] == "Here's the status: no blockers."
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["verb"] == "project_status"
    assert "tasks" in result["tool_results"][0]["result"]
    assert "blockers" in result["tool_results"][0]["result"]
    assert result["reactions"] == ["✅"]
    assert result["assumed"] is False
    assert len(caller.calls) == 2  # initial turn + one fold-in follow-up


# --- Malformed JSON on turn 1, valid on the corrective retry ----------------


def test_malformed_json_then_valid_on_retry_recovers(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller([
        "this is not json at all, sorry",
        _envelope(reply="Got it, all set.", tool_calls=[]),
    ])

    result = concierge.run_turn(
        "hello", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert result["reply"] == "Got it, all set."
    assert result["tool_results"] == []
    assert len(caller.calls) == 2
    # the corrective re-prompt names the JSON contract so the model can fix itself
    assert "JSON object" in caller.calls[1][1]


def test_malformed_json_on_both_attempts_falls_back_without_raising(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller(["garbage 1", "garbage 2"])

    result = concierge.run_turn(
        "hello", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    for verb in tools.TOOL_CATALOG:
        assert verb in result["reply"]
    assert result["tool_results"] == []
    assert result["reactions"] == []
    assert len(caller.calls) == 2  # exactly one corrective retry, not unbounded


# --- Unknown verb -> graceful "here's what I can do" fallback --------------


def test_unknown_verb_falls_back_to_catalog_listing_no_exception(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller([
        _envelope(reply="Sure, nuking it.", tool_calls=[{"verb": "nuke", "args": {}}]),
    ])

    result = concierge.run_turn(
        "please nuke the project", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    for verb in tools.TOOL_CATALOG:
        assert verb in result["reply"]
    assert result["reactions"] == []
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["error"] == "tool_not_allowed"


# --- assumed:true -> thinking-face reaction ---------------------------------


def test_assumed_true_sets_thinking_reaction(tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller([
        _envelope(
            reply="I'll assume you mean the bound project.",
            tool_calls=[], assumed=True,
        ),
    ])

    result = concierge.run_turn(
        "how's it going", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert result["reactions"] == ["🤔"]
    assert result["assumed"] is True


# --- CRITICAL INVARIANT: a C-class verb from text never executes -----------


def test_publish_pr_from_model_text_never_executes_and_needs_confirmation(tmp_path: Path) -> None:
    """A model envelope emitting {"verb": "publish_pr"} from plain chat text
    must produce a needs_confirmation outcome — the effect fn (publish_fn)
    must never run. This is the guarantee that untrusted Slack text can't
    spend money or open a PR: concierge.run_turn must always call
    tools.dispatch with confirmed_via=None (never "block_actions")."""
    store.bind_channel("C1", "proj-a")
    publish_calls: list[dict[str, Any]] = []
    deps = _deps(tmp_path, publish_fn=lambda args: publish_calls.append(args) or {"pr_url": "x"})
    caller = _ScriptedCaller([
        _envelope(
            reply="Publishing your PR now!",
            tool_calls=[{"verb": "publish_pr", "args": {"title": "hello"}}],
        ),
    ])

    result = concierge.run_turn(
        "please publish the PR", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller, max_hops=1,
    )

    assert publish_calls == []  # the effect fn was NEVER invoked
    assert len(result["tool_results"]) == 1
    dispatched = result["tool_results"][0]["result"]
    assert dispatched["status"] == "needs_confirmation"
    assert "confirmation_id" in dispatched

    staged = store.get_confirmation(dispatched["confirmation_id"])
    assert staged is not None
    assert staged["verb"] == "publish_pr"


def test_resolve_decision_from_model_text_never_executes(tmp_path: Path) -> None:
    """Same injection guard, for resolve_decision (the other verb the CARRY
    note in Task 4's review specifically flagged)."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path)
    caller = _ScriptedCaller([
        _envelope(
            reply="Accepting that change now.",
            tool_calls=[{
                "verb": "resolve_decision",
                "args": {"change_id": "chg-1", "decision": "accept"},
            }],
        ),
    ])

    result = concierge.run_turn(
        "accept the pending change", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller, max_hops=1,
    )

    dispatched = result["tool_results"][0]["result"]
    assert dispatched["status"] == "needs_confirmation"
    # _FakePmChanges.accept/decline raise AssertionError if ever invoked —
    # reaching this line at all is part of the proof they were not.
