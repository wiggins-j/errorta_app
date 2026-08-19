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


# The default PM member every test gets unless it overrides ``run_config`` —
# a real ``gateway_route_id`` so tests written before this seam existed keep
# exercising the caller exactly as before.
_DEFAULT_PM_MEMBER = {
    "member_id": "m-pm", "role": "answerer", "enabled": True,
    "gateway_route_id": "claude_cli.opus", "provider_kind": "cli",
    "metadata": {"coding_role": "pm"},
}


class FakeLedgerStore:
    def __init__(self, project_id: str, tmp_path: Path,
                 run_config: dict[str, Any] | None = None) -> None:
        self.project_id = project_id
        self.dir = tmp_path / f"ledger-{project_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._run_config = (
            run_config if run_config is not None
            else {"members": [dict(_DEFAULT_PM_MEMBER)]}
        )
        self._run_state: dict[str, Any] = {"status": "idle", "cancel_requested": False}

    def get_run_state(self) -> dict[str, Any]:
        return dict(self._run_state)

    def set_run_state(self, **patch: Any) -> dict[str, Any]:
        self._run_state.update(patch)
        return dict(self._run_state)

    def list_tasks(self) -> list[Any]:
        return []

    def list_turns(self) -> list[dict[str, Any]]:
        return []

    def list_decisions(self) -> list[dict[str, Any]]:
        return []

    def get_project(self) -> Any:
        raise RuntimeError("no project")

    def get_run_config(self) -> dict[str, Any]:
        return self._run_config

    def add_task(self, *, title: str, role: str, detail: str = "",
                 task_type: str = "implementation", **_: Any) -> FakeTask:
        return FakeTask("t-1")


class _FakePmChanges:
    def accept(self, ledger_store: Any, change_id: str) -> None:
        raise AssertionError("accept must never be called from an unconfirmed concierge turn")

    def decline(self, ledger_store: Any, change_id: str) -> None:
        raise AssertionError("decline must never be called from an unconfirmed concierge turn")


def _deps(tmp_path: Path, *, run_config: dict[str, Any] | None = None,
          **overrides: Any) -> tools.ToolDeps:
    kwargs: dict[str, Any] = {
        "store": store,
        "ledger_factory": lambda project_id: FakeLedgerStore(
            project_id, tmp_path, run_config=run_config),
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


def test_build_system_prompt_grounds_the_concierge_to_its_real_tools() -> None:
    """The PM confabulated a capability it has no tool for (staged a
    project-creation "Approve" flow that doesn't exist). The prompt must
    explicitly forbid claiming/staging/inventing any action outside the
    TOOL_CATALOG, name project creation/config as something it cannot do,
    and never say it can be done from Slack."""
    prompt = concierge.build_system_prompt("proj-a")

    lowered = prompt.lower()
    # Explicitly disclaims the verbs it does NOT have.
    assert "create" in lowered and "project" in lowered
    assert "north star" in lowered
    # Forbids claiming/staging actions it can't perform and inventing flows.
    assert "never claim" in lowered or "never imply" in lowered
    assert "invent" in lowered
    # Points elsewhere for what it can't do.
    assert "errorta app" in lowered


def test_build_system_prompt_grounding_lists_real_catalog_capabilities() -> None:
    """The "what I CAN do" list in the grounding rule must be derived from
    the live TOOL_CATALOG (not hand-typed prose) so it can't drift stale if
    the catalog changes — every R-trust verb's summary should appear."""
    prompt = concierge.build_system_prompt("proj-a")

    for verb, spec in tools.TOOL_CATALOG.items():
        if spec.get("trust") == "R":
            assert spec["summary"] in prompt


def test_grounding_no_longer_claims_it_cannot_reconfigure_a_team() -> None:
    """Slice 3: the concierge gained ``reconfigure_team`` (role -> model).
    The hand-written negative in the etiquette contract must no longer
    contradict that — the grounding rule may still deny creating, deleting,
    or renaming a PROJECT, but not deny reconfiguring the TEAM."""
    prompt = concierge.build_system_prompt("proj-a")
    lowered = prompt.lower()

    # The grounding rule's negative no longer lumps "team" in with the
    # things it truly can't do.
    assert "configure a project or team" not in lowered
    # It still truthfully can't create/delete/rename a project.
    assert "create, delete, or rename a project" in lowered
    # reconfigure_team's own catalog summary is present (mentions role and
    # model), so the prompt is not silently missing the capability either.
    assert "reconfigure_team" in prompt


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


# --- PM-member model route (concierge's "brain" uses the team's PM route) --


def test_run_turn_dispatches_through_the_pm_members_gateway_route(
        tmp_path: Path) -> None:
    """The concierge is not a persisted room member — it has no route of its
    own. It must borrow the project's configured PM member's
    ``gateway_route_id`` rather than a routeless synthetic identity that can
    never reach a real model."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path, run_config={"members": [
        {"member_id": "m-pm", "role": "answerer", "enabled": True,
         "gateway_route_id": "claude_cli.opus", "provider_kind": "cli",
         "metadata": {"coding_role": "pm"}},
    ]})
    caller = _ScriptedCaller([
        _envelope(reply="Here's the status: no blockers.", tool_calls=[]),
    ])

    result = concierge.run_turn(
        "how's it going", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert len(caller.calls) == 1
    member_used = caller.calls[0][0]
    assert member_used["gateway_route_id"] == "claude_cli.opus"
    assert result["reply"] == "Here's the status: no blockers."


def test_run_turn_with_no_pm_member_returns_clean_reply_never_calls_model(
        tmp_path: Path) -> None:
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path, run_config={"members": []})
    caller = _ScriptedCaller([])

    result = concierge.run_turn(
        "how's it going", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert len(caller.calls) == 0
    assert "proj-a" in result["reply"]
    assert "PM" in result["reply"]
    assert result["tool_results"] == []
    assert result["reactions"] == []
    assert result["assumed"] is False


def test_run_turn_with_empty_pm_route_returns_clean_reply_never_calls_model(
        tmp_path: Path) -> None:
    """A PM member exists but has no route wired (e.g. team configured but
    the PM's model was never assigned) — same clean-refusal outcome as no PM
    at all, not a crash inside the gateway on an empty route_id."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path, run_config={"members": [
        {"member_id": "m-pm", "role": "answerer", "enabled": True,
         "gateway_route_id": "", "metadata": {"coding_role": "pm"}},
    ]})
    caller = _ScriptedCaller([])

    result = concierge.run_turn(
        "how's it going", [],
        project_id="proj-a", channel_id="C1", thread_ts="t1",
        deps=deps, caller=caller,
    )

    assert len(caller.calls) == 0
    assert result["tool_results"] == []


# --- Slice 5 follow-up: autopilot-aware confirmation copy -------------------


def test_build_system_prompt_autopilot_off_tells_user_to_press_approve() -> None:
    prompt = concierge.build_system_prompt("proj-a")
    assert "press Approve" in prompt
    assert "Autopilot is ON" not in prompt


def test_build_system_prompt_autopilot_on_drops_press_approve() -> None:
    prompt = concierge.build_system_prompt("proj-a", autopilot=True)
    assert "Autopilot is ON" in prompt
    assert "someone needs to press Approve" not in prompt
    # the injection wall is unchanged in either mode
    assert "NEVER execute from chat text alone" in prompt


# --- Slice 4 §3.5: project goal state (north star, DoD, Current Focus) -----


def test_build_system_prompt_includes_north_star_dod_and_focus(tmp_path: Path) -> None:
    """Slice 4 §3.5: the Slack PM was blind to its own project's goal —
    pm_reference.build_live_state returns only routes/autonomy/governance/
    runtime/room, while the in-app PM chat injects north star, DoD and
    Current Focus. A PM asked "what's next" with none of that is guessing."""
    from errorta_council.coding.ledger import LedgerStore

    ledger = LedgerStore("proj-state")
    ledger.create_project(
        north_star="Teach fractions through a platformer.",
        definition_of_done="A playable level ships.",
        target="new", repo_path=None, delivery_root=None,
    )
    ledger.add_focus(title="Build the tick engine", body="task 14", origin="slack_pm")

    prompt = concierge.build_system_prompt("proj-state")

    assert "Teach fractions through a platformer." in prompt
    assert "A playable level ships." in prompt
    assert "Build the tick engine" in prompt
    assert "task 14" in prompt


def test_build_system_prompt_survives_an_unreadable_project() -> None:
    """The block must degrade to nothing, never raise — mirroring how
    runner._pm_prompt guards its own focus read (runner.py:3154-3157).
    A missing project must not take down the whole Slack turn."""
    prompt = concierge.build_system_prompt("no-such-project-at-all")

    assert "SLACK ETIQUETTE CONTRACT" in prompt


def test_grounding_no_longer_claims_it_cannot_set_a_north_star() -> None:
    """Slice 4: the concierge gained set_north_star and set_next_goal. The
    hand-written negative must no longer contradict that — a model told it
    has "NO tool to set a north star" will keep refusing to use the tool it
    now has. Mirrors the Slice 3 reconfigure_team fix."""
    prompt = concierge.build_system_prompt("proj-a")
    lowered = prompt.lower()

    assert "no tool to create, delete, or rename a project, or set a north" not in lowered
    # It still truthfully can't create/delete/rename a project.
    assert "create, delete, or rename a project" in lowered
    # And the new capabilities are named.
    assert "set_north_star" in prompt
    assert "set_next_goal" in prompt
    assert "propose_next_goal" in prompt


def _propose_envelope() -> str:
    return json.dumps(
        {"reply": "", "tool_calls": [{"verb": "propose_next_goal", "args": {}}]}
    )


def test_run_turn_wires_a_model_caller_into_propose_next_goal(tmp_path: Path) -> None:
    """Regression (branch review #1): ``ToolDeps.goal_caller`` was connected
    nowhere. The only production construction site (slack_lifecycle) builds a
    bare ``ToolDeps()``, and ``run_turn`` passed ``deps`` through untouched —
    so the branch's headline read verb returned "no model is wired up" on
    every real invocation and the whole repo-grounded proposal feature was
    dead code in the shipped bridge. Every existing test injected a
    ``goal_caller`` by hand, which is exactly what hid it.

    This test builds the deps the way production does — WITHOUT a
    ``goal_caller`` — and asserts the turn supplies one."""
    store.bind_channel("C1", "proj-a")

    seen: dict[str, Any] = {}

    def spy_propose(store_: Any, *, member: dict[str, Any], caller: Any) -> dict[str, Any]:
        seen["member"] = member
        seen["caller"] = caller
        return {"title": "Ship the reducer", "body": "scope",
                "evidence": ["a.py"], "stale": False}

    deps = _deps(tmp_path, propose_goal_fn=spy_propose)
    assert deps.goal_caller is None, "this test only means something on a bare ToolDeps"

    caller = _ScriptedCaller([
        _propose_envelope(),
        json.dumps({"reply": "Here's what I'd work on next."}),
    ])

    result = concierge.run_turn(
        "what should we work on next?", [], project_id="proj-a",
        channel_id="C1", thread_ts="1.0", deps=deps, caller=caller,
    )

    tool_result = result["tool_results"][0]["result"]
    assert tool_result["status"] == "proposed", tool_result
    assert tool_result["title"] == "Ship the reducer"
    assert seen["caller"] is caller
    # And the route came from the run config, not from the model's args.
    assert seen["member"]["gateway_route_id"] == "claude_cli.opus"


def test_run_turn_never_mutates_the_shared_deps(tmp_path: Path) -> None:
    """``deps`` is built once at bridge start and shared by every concurrently
    running thread. Wiring the caller by assignment would publish one turn's
    model to every other project's turn; the wiring must be per-turn."""
    store.bind_channel("C1", "proj-a")
    deps = _deps(tmp_path, propose_goal_fn=lambda store_, **kw: {
        "title": "t", "body": "b", "evidence": [], "stale": False})
    caller = _ScriptedCaller([
        _propose_envelope(),
        json.dumps({"reply": "done"}),
    ])

    concierge.run_turn(
        "what next?", [], project_id="proj-a", channel_id="C1",
        thread_ts="1.0", deps=deps, caller=caller,
    )

    assert deps.goal_caller is None
