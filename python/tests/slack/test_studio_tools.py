"""Task 4 tests: studio tool surface (`errorta_slack.studio_tools`).

The load-bearing tests are the injection-guard ones: a text-supplied
``confirmed_via=None`` call to ``create_project`` must NEVER reach
``create_fn``/``provision_fn``/``store.bind_channel`` — only a verified
``confirmed_via="block_actions"`` re-entry (the provenance marker set by a
real Slack block_actions callback) may. Every fixture here uses placeholder
ids/charters only (this is a PUBLIC repo).
"""
from __future__ import annotations

from typing import Any

import pytest

from errorta_slack import provisioning, studio_tools

# --- Fakes ---------------------------------------------------------------


class FakeStore:
    def __init__(self, *, events: list[str] | None = None) -> None:
        self.staged: list[tuple[str, dict[str, Any], str, str]] = []
        self.bound: list[tuple[str, str]] = []
        self.unbound: list[str] = []
        self.channel_map: dict[str, str] = {}
        self._next_cid = 0
        self._events = events
        self.cursors: dict[str, str] = {}

    def stage_confirmation(self, verb: str, args: dict[str, Any], thread_ts: str, *,
                            channel_id: str = "") -> str:
        # channel_id is load-bearing in the real store: outbound.sweep_timeouts
        # reads it off every pending confirmation record to post its timeout
        # auto-decision back to the right channel. Recording it here (rather
        # than accepting **kwargs and dropping it) is what would have caught
        # the studio_tools.dispatch call that originally omitted it.
        self.staged.append((verb, args, thread_ts, channel_id))
        self._next_cid += 1
        return f"cid-{self._next_cid}"

    def bind_channel(self, channel_id: str, project_id: str) -> None:
        self.bound.append((channel_id, project_id))

    def get_cursor(self, channel_id: str) -> str | None:
        return self.cursors.get(channel_id)

    def advance_cursor(self, channel_id: str, marker: str) -> None:
        self.cursors[channel_id] = marker

    def channel_for_project(self, project_id: str) -> str | None:
        return self.channel_map.get(project_id)

    def unbind(self, channel_id: str) -> None:
        self.unbound.append(channel_id)
        if self._events is not None:
            self._events.append("unbind")


class FakeLedger:
    """Fake of the ``LedgerStore(project_id)`` object ``deps.ledger_factory``
    returns — tracks run-state and status writes for the spin-down verb."""

    def __init__(self, project_id: str, *, run_status: str = "idle",
                 events: list[str] | None = None) -> None:
        self.project_id = project_id
        self._run_state = {"status": run_status}
        self.status_calls: list[str] = []
        self.run_state_patches: list[dict[str, Any]] = []
        self._events = events

    def get_run_state(self) -> dict[str, Any]:
        return dict(self._run_state)

    def set_run_state(self, **patch: Any) -> dict[str, Any]:
        self.run_state_patches.append(patch)
        self._run_state.update(patch)
        if self._events is not None:
            self._events.append("set_run_state")
        return dict(self._run_state)

    def set_project_status(self, status: str) -> None:
        self.status_calls.append(status)
        if self._events is not None:
            self._events.append(f"set_project_status:{status}")

    # --- Slice 4: adopt_project reads/writes these -------------------------
    def get_project(self) -> Any:
        if getattr(self, "missing", False):
            from errorta_council.coding.ledger import ProjectNotFound

            raise ProjectNotFound(f"no project: {self.project_id}")
        return FakeProject(self.project_id)

    def get_run_config(self) -> dict[str, Any]:
        return {"members": list(getattr(self, "members", []))}

    def set_run_config(self, *, room_id: Any = None,
                       members: list[dict[str, Any]] | None = None) -> None:
        self.run_config_calls = getattr(self, "run_config_calls", [])
        self.run_config_calls.append({"room_id": room_id, "members": members})
        self.members = list(members or [])

    def active_focuses(self) -> list[Any]:
        return list(getattr(self, "focuses", []))


class FakeProject:
    def __init__(self, project_id: str) -> None:
        self.id = project_id
        self.north_star = "Teach fractions through a platformer."
        self.definition_of_done = "A playable level ships."
        self.work_request = ""
        self.repo_path = None


def _charter(**overrides: Any) -> dict[str, Any]:
    charter = {
        "title": "Homeschool Game!",
        "north_star": "Teach fractions through a platformer.",
        "definition_of_done": "A playable level ships.",
        "audience": "kids",
        "modality": "web",
        "entrypoint": "index.html",
        "team_recipe": "solo",
        "autonomous": False,
    }
    charter.update(overrides)
    return charter


def _recording_create_fn(calls: list[tuple[str, dict[str, Any]]]) -> Any:
    def _create_fn(project_id: str, charter: dict[str, Any], *,
                    available_routes: Any = None, members: Any = None) -> FakeProject:
        calls.append((project_id, charter))
        return FakeProject(project_id)
    return _create_fn


def _recording_create_fn_kwargs(calls: list[dict[str, Any]]) -> Any:
    """Like ``_recording_create_fn`` but records the full kwargs each call
    was made with, for tests asserting on ``members``/``available_routes``."""
    def _create_fn(project_id: str, charter: dict[str, Any], *,
                    available_routes: Any = None, members: Any = None) -> FakeProject:
        calls.append({
            "project_id": project_id,
            "charter": charter,
            "available_routes": available_routes,
            "members": members,
        })
        return FakeProject(project_id)
    return _create_fn


def _recording_provision_fn(calls: list[dict[str, Any]], *,
                             channel_id: str = "C-NEW", name: str = "homeschool-game") -> Any:
    def _provision_fn(web_client: Any, *, title: str, invite_user_ids: list[str],
                       purpose: str = "") -> dict[str, Any]:
        calls.append({"title": title, "invite_user_ids": invite_user_ids, "purpose": purpose})
        return {"channel_id": channel_id, "name": name}
    return _provision_fn


def _deps(**overrides: Any) -> studio_tools.StudioDeps:
    base: dict[str, Any] = dict(
        store=FakeStore(),
        list_projects_fn=lambda: [],
        create_fn=_recording_create_fn([]),
        provision_fn=_recording_provision_fn([]),
    )
    base.update(overrides)
    return studio_tools.StudioDeps(**base)


# --- Injection guard: the whole point -------------------------------------


def test_create_project_unconfirmed_stages_and_never_touches_engine() -> None:
    create_calls: list[tuple[str, dict[str, Any]]] = []
    provision_calls: list[dict[str, Any]] = []
    store = FakeStore()
    deps = studio_tools.StudioDeps(
        store=store,
        create_fn=_recording_create_fn(create_calls),
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert result["confirmation_id"] == "cid-1"
    assert create_calls == []
    assert provision_calls == []
    assert store.bound == []
    assert store.staged == [("create_project", _charter(), "t1", "C1")]


def test_create_project_confirmed_via_block_actions_executes_in_order() -> None:
    create_calls: list[tuple[str, dict[str, Any]]] = []
    provision_calls: list[dict[str, Any]] = []
    store = FakeStore()
    deps = studio_tools.StudioDeps(
        store=store,
        create_fn=_recording_create_fn(create_calls),
        provision_fn=_recording_provision_fn(provision_calls),
        invite_user_ids=["U1", "U2"],
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert result["project_id"] == "homeschool-game"
    assert result["channel_id"] == "C-NEW"
    assert result["channel_name"] == "homeschool-game"

    # create_fn ran before provision_fn ran before bind_channel.
    assert len(create_calls) == 1
    assert create_calls[0][0] == "homeschool-game"
    assert len(provision_calls) == 1
    assert provision_calls[0]["invite_user_ids"] == ["U1", "U2"]
    assert store.bound == [("C-NEW", "homeschool-game")]
    assert store.staged == []


def test_create_project_confirmed_via_wrong_provenance_still_stages() -> None:
    # Any provenance string other than the literal "block_actions" marker
    # (e.g. a value an attacker could try to smuggle through chat text or
    # forged args) must be treated exactly like confirmed_via=None.
    create_calls: list[tuple[str, dict[str, Any]]] = []
    deps = _deps(create_fn=_recording_create_fn(create_calls))

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="chat_text", deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert create_calls == []


# --- Default team: bypass the unavailable-routes probe --------------------
#
# fix/slack-studio-default-team: `deps.available_routes` defaults to None,
# which makes `create_project_from_charter` call the live
# `pm_reference.list_available_routes()` — on a machine where the desktop
# app's Test probe hasn't marked claude_cli/cursor_cli "connected", that
# returns only `custom.senditai`, so `resolve_team` builds the wrong (or an
# empty) team and the spun-up project's PM can't work. The studio must
# instead pass an explicit `members=` team, which `create_project_from_charter`
# sets verbatim (bypassing resolve_team + available_routes entirely).


def test_create_project_passes_config_default_team_as_members() -> None:
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert len(create_calls) == 1
    members = create_calls[0]["members"]
    assert members == [
        {
            "id": "pm-1", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "pm"},
            "gateway_route_id": "claude_cli.opus", "provider_kind": "claude_cli",
        },
        {
            "id": "dev-1", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "dev"},
            "gateway_route_id": "cursor_cli.composer-2.5", "provider_kind": "cursor_cli",
        },
        {
            "id": "dev-2", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "dev"},
            "gateway_route_id": "cursor_cli.composer-2.5", "provider_kind": "cursor_cli",
        },
        {
            "id": "dev-3", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "dev"},
            "gateway_route_id": "cursor_cli.composer-2.5", "provider_kind": "cursor_cli",
        },
        {
            "id": "reviewer-1", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "reviewer"},
            "gateway_route_id": "claude_cli.sonnet", "provider_kind": "claude_cli",
        },
        {
            "id": "tester-1", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "tester"},
            "gateway_route_id": "claude_cli.sonnet", "provider_kind": "claude_cli",
        },
    ]
    # available_routes is still threaded through (harmless -- ignored by
    # create_project_from_charter whenever members is non-empty).
    assert create_calls[0]["available_routes"] == deps.available_routes


def test_create_project_honors_injected_default_team_over_config() -> None:
    create_calls: list[dict[str, Any]] = []
    deps = _deps(
        create_fn=_recording_create_fn_kwargs(create_calls),
        default_team=[{"coding_role": "pm", "gateway_route_id": "x.y"}],
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert create_calls[0]["members"] == [
        {
            "id": "pm-1", "role": "answerer", "enabled": True,
            "model_mode": "single", "metadata": {"coding_role": "pm"},
            "gateway_route_id": "x.y", "provider_kind": "x",
        },
    ]


def test_default_team_members_expands_role_route_specs() -> None:
    specs = [
        {"coding_role": "pm", "gateway_route_id": "claude_cli.opus"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
        {"coding_role": "dev", "gateway_route_id": "cursor_cli.composer-2.5"},
    ]

    members = studio_tools._default_team_members(specs)

    assert [m["id"] for m in members] == ["pm-1", "dev-1", "dev-2"]
    assert members[0]["provider_kind"] == "claude_cli"
    assert members[0]["metadata"] == {"coding_role": "pm"}
    assert members[1]["gateway_route_id"] == "cursor_cli.composer-2.5"
    for m in members:
        assert m["role"] == "answerer"
        assert m["enabled"] is True
        assert m["model_mode"] == "single"


# --- Fail-closed: unknown verb ---------------------------------------------


def test_unknown_verb_raises_tool_not_allowed_naming_catalog() -> None:
    deps = _deps()
    with pytest.raises(studio_tools.ToolError) as excinfo:
        studio_tools.dispatch(
            "delete_all", {}, channel_id="C1", thread_ts="t1", deps=deps,
        )
    assert excinfo.value.code == "tool_not_allowed"
    message = str(excinfo.value)
    for verb in studio_tools.TOOL_CATALOG:
        assert verb in message


# --- Channel-fail path -------------------------------------------------


def test_channel_provisioning_failure_after_project_created_returns_error() -> None:
    create_calls: list[tuple[str, dict[str, Any]]] = []
    store = FakeStore()

    def _failing_provision_fn(web_client: Any, *, title: str,
                               invite_user_ids: list[str], purpose: str = "") -> dict[str, Any]:
        raise provisioning.ProvisioningError("missing_scope", "conversations_create failed")

    deps = studio_tools.StudioDeps(
        store=store,
        create_fn=_recording_create_fn(create_calls),
        provision_fn=_failing_provision_fn,
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert result["project_id"] == "homeschool-game"
    assert "missing_scope" in result["detail"] or "conversations_create" in result["detail"]
    assert store.bound == []
    # The project itself was created — it isn't lost, just not yet channeled.
    assert len(create_calls) == 1


# --- Missing charter field --------------------------------------------


def test_create_fn_value_error_returns_clean_error_not_crash() -> None:
    def _raising_create_fn(project_id: str, charter: dict[str, Any], *,
                            available_routes: Any = None, members: Any = None) -> Any:
        raise ValueError("charter missing required field: 'north_star'")

    provision_calls: list[dict[str, Any]] = []
    deps = studio_tools.StudioDeps(
        store=FakeStore(),
        create_fn=_raising_create_fn,
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "north_star" in result["detail"]
    # provision_fn must not run once create_fn has failed.
    assert provision_calls == []


def test_create_fn_arbitrary_exception_returns_clean_error_not_crash() -> None:
    # Any engine exception beyond the two known-safe shapes (ValueError,
    # LedgerError) — e.g. OSError from workspace I/O — must still be caught
    # by dispatch rather than blow up a live Slack turn. The returned detail
    # must not leak the raw exception message (which could carry a path or
    # other internal detail), only the exception's type name.
    def _raising_create_fn(project_id: str, charter: dict[str, Any], *,
                            available_routes: Any = None, members: Any = None) -> Any:
        raise OSError("disk")

    store = FakeStore()
    provision_calls: list[dict[str, Any]] = []
    deps = studio_tools.StudioDeps(
        store=store,
        create_fn=_raising_create_fn,
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "disk" not in result["detail"]
    assert "OSError" in result["detail"]
    assert provision_calls == []
    assert store.bound == []


# --- list_projects ---------------------------------------------------------


def test_list_projects_returns_injected_list() -> None:
    projects = [{"id": "p1", "north_star": "n1"}, {"id": "p2", "north_star": "n2"}]
    deps = _deps(list_projects_fn=lambda: projects)

    result = studio_tools.dispatch(
        "list_projects", {}, channel_id="C1", thread_ts="t1", deps=deps,
    )

    assert result == {"projects": projects}


# --- answer_question ---------------------------------------------------


def test_answer_question_returns_ok_no_side_effect() -> None:
    deps = _deps()
    result = studio_tools.dispatch(
        "answer_question", {"question": "what is this?"},
        channel_id="C1", thread_ts="t1", deps=deps,
    )
    assert result == {"status": "ok"}


# --- _project_id_from_title -------------------------------------------


def test_project_id_from_title_slugifies() -> None:
    pid = studio_tools._project_id_from_title("Homeschool Game!")
    assert pid == "homeschool-game"
    assert "/" not in pid
    assert ".." not in pid


def test_project_id_from_title_falls_back_when_empty() -> None:
    assert studio_tools._project_id_from_title("") == "project"
    assert studio_tools._project_id_from_title("   ") == "project"


def test_project_id_from_title_never_dot_or_dotdot() -> None:
    assert studio_tools._project_id_from_title(".") == "project"
    assert studio_tools._project_id_from_title("..") == "project"
    assert studio_tools._project_id_from_title("...") == "project"


def test_project_id_from_title_truncates_to_64_chars() -> None:
    pid = studio_tools._project_id_from_title("a" * 100)
    assert len(pid) <= 64


# --- archive_project (Task 2): the spin-down C-class verb -----------------
#
# Trust class C, same non-negotiable injection discipline as create_project:
# a chat-text call (confirmed_via=None) must stage a confirmation and touch
# NONE of set_run_state/set_project_status/archive/unbind. Only a verified
# confirmed_via="block_actions" re-entry may execute the real effect.


def test_archive_project_trust_class_is_c() -> None:
    assert studio_tools.TOOL_CATALOG["archive_project"]["trust"] == "C"


def test_archive_project_unconfirmed_stages_and_never_touches_engine() -> None:
    events: list[str] = []
    store = FakeStore(events=events)
    store.channel_map["p1"] = "C1"
    ledger = FakeLedger("p1", run_status="running", events=events)
    archive_calls: list[tuple[Any, str]] = []

    def _archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        archive_calls.append((web_client, channel_id))
        return {"channel_id": channel_id, "archived": True}

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert result["confirmation_id"] == "cid-1"
    assert ledger.status_calls == []
    assert ledger.run_state_patches == []
    assert archive_calls == []
    assert store.unbound == []
    assert events == []


def test_archive_project_confirmed_via_wrong_provenance_still_stages() -> None:
    ledger = FakeLedger("p1")
    deps = studio_tools.StudioDeps(store=FakeStore(), ledger_factory=lambda pid: ledger)

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="chat_text", deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert ledger.status_calls == []


def test_archive_project_confirmed_running_project_cancels_pauses_archives_unbinds_in_order() -> None:
    events: list[str] = []
    store = FakeStore(events=events)
    store.channel_map["p1"] = "C1"
    ledger = FakeLedger("p1", run_status="running", events=events)

    def _archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        events.append("archive")
        return {"channel_id": channel_id, "archived": True}

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result == {"status": "archived", "project_id": "p1", "channel_id": "C1"}
    assert ledger.run_state_patches == [{"cancel_requested": True}]
    assert events == ["set_run_state", "set_project_status:paused", "archive", "unbind"]
    assert store.unbound == ["C1"]


def test_archive_project_confirmed_non_running_project_skips_cancel() -> None:
    events: list[str] = []
    store = FakeStore(events=events)
    store.channel_map["p1"] = "C1"
    ledger = FakeLedger("p1", run_status="idle", events=events)

    def _archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        events.append("archive")
        return {"channel_id": channel_id, "archived": True}

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "archived"
    assert ledger.run_state_patches == []
    assert events == ["set_project_status:paused", "archive", "unbind"]


def test_archive_project_confirmed_no_bound_channel_pauses_only() -> None:
    store = FakeStore()  # no channel bound for p1
    ledger = FakeLedger("p1", run_status="idle")
    archive_calls: list[str] = []

    def _archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        archive_calls.append(channel_id)
        return {"channel_id": channel_id, "archived": True}

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result == {"status": "archived", "project_id": "p1", "channel_id": None}
    assert ledger.status_calls == ["paused"]
    assert archive_calls == []
    assert store.unbound == []


def test_archive_project_confirmed_archive_provisioning_error_returns_clean_error_no_unbind() -> None:
    store = FakeStore()
    store.channel_map["p1"] = "C1"
    ledger = FakeLedger("p1", run_status="idle")

    def _failing_archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        raise provisioning.ProvisioningError("missing_scope", "conversations_archive failed")

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_failing_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert result["project_id"] == "p1"
    assert "missing_scope" in result["detail"] or "conversations_archive" in result["detail"]
    # The project may already be paused by the time the archive call fails —
    # that's fine (soft spin-down is idempotent on retry); what must NOT
    # happen is unbinding a channel that never actually got archived.
    assert ledger.status_calls == ["paused"]
    assert store.unbound == []


def test_archive_project_confirmed_archive_arbitrary_exception_returns_clean_error() -> None:
    store = FakeStore()
    store.channel_map["p1"] = "C1"
    ledger = FakeLedger("p1")

    def _raising_archive_fn(web_client: Any, channel_id: str) -> dict[str, Any]:
        raise RuntimeError("boom, path=/secret/workspace")

    deps = studio_tools.StudioDeps(
        store=store, ledger_factory=lambda pid: ledger, provision_archive_fn=_raising_archive_fn,
    )

    result = studio_tools.dispatch(
        "archive_project", {"project_id": "p1"}, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "/secret/workspace" not in result["detail"]
    assert "RuntimeError" in result["detail"]
    assert store.unbound == []


# --- charter-specified team (honor the models the user asked for) -----------


def test_create_project_honors_charter_team_of_uniform_opus() -> None:
    """The user said 'Opus for all roles' — create_project must build the team
    from the charter's `team`, not silently use the mixed default."""
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))
    team = [{"coding_role": r, "gateway_route_id": "claude_cli.opus"}
            for r in ("pm", "dev", "dev", "dev", "reviewer", "tester")]

    result = studio_tools.dispatch(
        "create_project", _charter(team=team), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    members = create_calls[0]["members"]
    assert [m["gateway_route_id"] for m in members] == ["claude_cli.opus"] * 6
    assert [m["metadata"]["coding_role"] for m in members] == \
        ["pm", "dev", "dev", "dev", "reviewer", "tester"]
    assert [m["id"] for m in members] == \
        ["pm-1", "dev-1", "dev-2", "dev-3", "reviewer-1", "tester-1"]


def test_create_project_charter_team_unknown_route_falls_back_to_default() -> None:
    """Grounded-or-fall-back: a route the operator hasn't configured makes the
    WHOLE custom team fall back to the known-good default rather than shipping
    a broken/hallucinated team."""
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))
    team = [{"coding_role": "pm", "gateway_route_id": "made_up.route"}]

    result = studio_tools.dispatch(
        "create_project", _charter(team=team), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    routes = [m["gateway_route_id"] for m in create_calls[0]["members"]]
    assert routes == [
        "claude_cli.opus", "cursor_cli.composer-2.5", "cursor_cli.composer-2.5",
        "cursor_cli.composer-2.5", "claude_cli.sonnet", "claude_cli.sonnet",
    ]


def test_create_project_charter_team_validated_against_effective_default_routes() -> None:
    """The allowed route set is whatever team is actually in effect (an
    injected default_team here) — a charter team using those routes is honored."""
    create_calls: list[dict[str, Any]] = []
    deps = _deps(
        create_fn=_recording_create_fn_kwargs(create_calls),
        default_team=[{"coding_role": "pm", "gateway_route_id": "x.y"}],
    )
    team = [{"coding_role": "pm", "gateway_route_id": "x.y"},
            {"coding_role": "dev", "gateway_route_id": "x.y"}]

    result = studio_tools.dispatch(
        "create_project", _charter(team=team), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert [m["gateway_route_id"] for m in create_calls[0]["members"]] == ["x.y", "x.y"]


def test_create_project_no_charter_team_still_uses_default() -> None:
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))

    studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    routes = [m["gateway_route_id"] for m in create_calls[0]["members"]]
    assert routes[0] == "claude_cli.opus" and "cursor_cli.composer-2.5" in routes


# --- designer seating by modality (spec §1 gate, studio path) ---------------


def _member_roles(members: list[dict[str, Any]]) -> list[str]:
    return [m["metadata"]["coding_role"] for m in members]


def test_create_project_seats_designer_for_ui_modality() -> None:
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))

    studio_tools.dispatch(
        "create_project", _charter(modality="static"), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    members = create_calls[0]["members"]
    assert "designer" in _member_roles(members)
    designer = next(m for m in members if m["metadata"]["coding_role"] == "designer")
    assert designer["gateway_route_id"] == "claude_cli.opus"
    assert designer["id"] == "designer-1"


def test_create_project_strips_designer_for_cli_modality() -> None:
    """Modality gate: cli/binary/container get NO designer — the whole design
    spec must stay provably inert for non-UI projects."""
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))

    for modality in ("cli", "binary", "container"):
        create_calls.clear()
        studio_tools.dispatch(
            "create_project", _charter(modality=modality), channel_id="C1", thread_ts="t1",
            confirmed_via="block_actions", deps=deps,
        )
        assert "designer" not in _member_roles(create_calls[0]["members"]), modality


def test_create_project_appends_designer_to_charter_team_for_ui() -> None:
    """Even when the user dictates the team ('Opus for all roles'), a UI
    project still gets a Designer seated (on the configured designer route)."""
    create_calls: list[dict[str, Any]] = []
    deps = _deps(create_fn=_recording_create_fn_kwargs(create_calls))
    team = [{"coding_role": r, "gateway_route_id": "claude_cli.opus"}
            for r in ("pm", "dev", "dev", "dev", "reviewer", "tester")]

    studio_tools.dispatch(
        "create_project", _charter(team=team, modality="server"),
        channel_id="C1", thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    roles = _member_roles(create_calls[0]["members"])
    assert roles.count("designer") == 1
    designer = next(m for m in create_calls[0]["members"] if m["metadata"]["coding_role"] == "designer")
    assert designer["gateway_route_id"] == "claude_cli.opus"


# --- Slice 4: adopt_project -------------------------------------------------


def test_adopt_project_provisions_binds_and_reports() -> None:
    """Spec §3.1: the studio could only open a channel for a project it just
    created (create_project is the only caller of create_project_channel), so
    an existing project like abovo could never get one."""
    provision_calls: list[dict[str, Any]] = []
    ledger = FakeLedger("abovo")
    ledger.members = [{"id": "pm-1"}]
    ledger.focuses = [object()]
    fake_store = FakeStore()
    deps = studio_tools.StudioDeps(
        store=fake_store,
        ledger_factory=lambda pid: ledger,
        provision_fn=_recording_provision_fn(provision_calls, channel_id="C-ABOVO",
                                             name="abovo"),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "adopted"
    assert result["channel_id"] == "C-ABOVO"
    assert result["team_seated"] is False        # abovo already has a team
    assert fake_store.bound == [("C-ABOVO", "abovo")]
    assert len(provision_calls) == 1


def test_adopt_project_is_idempotent_when_already_bound() -> None:
    """Re-running must NOT spawn a duplicate channel."""
    provision_calls: list[dict[str, Any]] = []
    fake_store = FakeStore()
    fake_store.channel_map["abovo"] = "C-EXISTING"
    deps = studio_tools.StudioDeps(
        store=fake_store,
        ledger_factory=lambda pid: FakeLedger(pid),
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "already_bound"
    assert result["channel_id"] == "C-EXISTING"
    assert provision_calls == []
    assert fake_store.bound == []


def test_adopt_project_refuses_an_unknown_project() -> None:
    provision_calls: list[dict[str, Any]] = []
    ledger = FakeLedger("nope")
    ledger.missing = True
    deps = studio_tools.StudioDeps(
        store=FakeStore(), ledger_factory=lambda pid: ledger,
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "nope"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert "nope" in result["detail"]
    assert provision_calls == []


def test_adopt_project_seats_a_team_only_when_there_is_none() -> None:
    ledger = FakeLedger("teamless")
    ledger.members = []
    deps = studio_tools.StudioDeps(
        store=FakeStore(), ledger_factory=lambda pid: ledger,
        provision_fn=_recording_provision_fn([]),
        default_team=[
            {"coding_role": "pm", "gateway_route_id": "claude_cli.opus"},
            {"coding_role": "dev", "gateway_route_id": "claude_cli.opus"},
            {"coding_role": "designer", "gateway_route_id": "claude_cli.opus"},
        ],
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "teamless"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["team_seated"] is True
    seated = ledger.run_config_calls[0]["members"]
    roles = [m["metadata"]["coding_role"] for m in seated]
    assert "pm" in roles and "dev" in roles
    # No stored modality -> non-UI -> the Designer is stripped, so the
    # design spec stays provably inert (Designer Slice 1 §1).
    assert "designer" not in roles


def test_adopt_project_start_is_refused_without_a_goal_but_still_binds() -> None:
    """A refused start is not a failed adoption: the channel stays bound and
    the result reports the refusal (spec §3.1 step 6)."""
    start_calls: list[str] = []
    ledger = FakeLedger("abovo")
    ledger.members = [{"id": "pm-1"}]
    ledger.focuses = []              # the real abovo: no goal at all
    fake_store = FakeStore()
    deps = studio_tools.StudioDeps(
        store=fake_store, ledger_factory=lambda pid: ledger,
        provision_fn=_recording_provision_fn([], channel_id="C-ABOVO"),
        start_run_fn=lambda pid, **kw: start_calls.append(pid),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo", "start": True},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "adopted"
    assert result["started"] is False
    assert "goal" in result["start_refused"].lower()
    assert start_calls == []
    assert fake_store.bound == [("C-ABOVO", "abovo")]


def test_adopt_project_starts_when_a_goal_exists() -> None:
    start_calls: list[str] = []
    ledger = FakeLedger("ready")
    ledger.members = [{"id": "pm-1"}]
    ledger.focuses = [object()]
    deps = studio_tools.StudioDeps(
        store=FakeStore(), ledger_factory=lambda pid: ledger,
        provision_fn=_recording_provision_fn([]),
        start_run_fn=lambda pid, **kw: start_calls.append(pid),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "ready", "start": True},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["started"] is True
    assert result["start_refused"] is None
    assert start_calls == ["ready"]


def test_adopt_project_from_chat_text_only_stages() -> None:
    """The injection wall: pasted Slack text must never create a public
    channel. Only a verified block_actions click may."""
    provision_calls: list[dict[str, Any]] = []
    fake_store = FakeStore()
    deps = studio_tools.StudioDeps(
        store=fake_store, ledger_factory=lambda pid: FakeLedger(pid),
        provision_fn=_recording_provision_fn(provision_calls),
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via=None, deps=deps,
    )

    assert result["status"] == "needs_confirmation"
    assert provision_calls == []
    assert fake_store.bound == []


def test_adopt_project_surfaces_a_provisioning_failure_without_binding() -> None:
    ledger = FakeLedger("abovo")
    ledger.members = [{"id": "pm-1"}]
    fake_store = FakeStore()

    def _boom(web_client: Any, **kwargs: Any) -> dict[str, Any]:
        raise provisioning.ProvisioningError("missing_scope", "needs channels:manage")

    deps = studio_tools.StudioDeps(
        store=fake_store, ledger_factory=lambda pid: ledger, provision_fn=_boom,
    )

    result = studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo"},
        channel_id="C-STUDIO", thread_ts="1.0",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "error"
    assert result["project_id"] == "abovo"
    assert fake_store.bound == []


def test_studio_deps_start_run_fn_defaults_to_none() -> None:
    """Must default to None so the lazy `errorta_app.routes.coding` import in
    tools._default_start_run never happens at StudioDeps() construction."""
    assert studio_tools.StudioDeps().start_run_fn is None


# --------------------------------------------------------------------------
# Slice 5a Task 2 — create starts the run.
#
# Operator decision (spec §3.2): creating a project from Slack ALWAYS starts
# it, with no approval gate of its own and regardless of autopilot. The
# `start=True` opt-in that `adopt_project` carries deliberately has no
# equivalent here.
# --------------------------------------------------------------------------


def _start_recorder(calls: list[dict[str, Any]]) -> Any:
    def _start_run_fn(project_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"project_id": project_id, **kwargs})
        return {"started": True}
    return _start_run_fn


def test_create_project_starts_the_run() -> None:
    start_calls: list[dict[str, Any]] = []
    deps = _deps(start_run_fn=_start_recorder(start_calls))

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert result["started"] is True
    assert result["start_refused"] is None
    # Fresh mode is mandatory: a new project is "idle", and continue_ 409s
    # ("run is not continuable") on anything but "stopped".
    assert len(start_calls) == 1
    assert start_calls[0]["resume"] is False
    assert start_calls[0]["continue_"] is False


def test_create_project_start_is_unconditional() -> None:
    """No `start` arg anywhere -- unlike adopt_project, create needs no opt-in."""
    start_calls: list[dict[str, Any]] = []
    charter = _charter()
    assert "start" not in charter
    deps = _deps(start_run_fn=_start_recorder(start_calls))

    studio_tools.dispatch(
        "create_project", charter, channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert len(start_calls) == 1


def test_create_project_start_failure_is_reported_not_raised() -> None:
    """A logged-out provider 409s the member-health preflight. The project and
    its binding must survive, and the result must carry the real reason."""
    def _boom(project_id: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("provider logged out")

    store = FakeStore()
    deps = _deps(store=store, start_run_fn=_boom)

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=deps,
    )

    assert result["status"] == "created"
    assert result["started"] is False
    assert result["start_refused"]
    # Never leak the exception message -- only its type name.
    assert "provider logged out" not in result["start_refused"]
    assert result["project_id"]
    assert result["channel_id"]
    assert store.bound, "the channel binding must survive a failed start"


def test_create_project_start_failure_is_classified_not_type_named() -> None:
    """Review fix: a logged-out provider must say so.

    `_classify_start_exception` already turns the two realistic fresh-start 409s
    into actionable text; rendering `type(exc).__name__` threw that away and
    told the operator "HTTPException", which they cannot act on.
    """
    class _Http409(Exception):
        status_code = 409
        detail = {"code": "member_health_preflight_failed",
                  "message": "claude_cli is not logged in"}

    def _boom(project_id: str, **kwargs: Any) -> dict[str, Any]:
        raise _Http409()

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=_deps(start_run_fn=_boom),
    )

    assert result["started"] is False
    assert "logged out" in result["start_refused"]
    assert "HTTPException" not in result["start_refused"]
    assert "_Http409" not in result["start_refused"]


def test_create_project_run_setup_required_is_named() -> None:
    class _Http409(Exception):
        status_code = 409
        detail = {"code": "run_setup_required", "message": "nope"}

    def _boom(project_id: str, **kwargs: Any) -> dict[str, Any]:
        raise _Http409()

    result = studio_tools.dispatch(
        "create_project", _charter(), channel_id="C1", thread_ts="t1",
        confirmed_via="block_actions", deps=_deps(start_run_fn=_boom),
    )

    assert result["start_refused"] == "the team isn't configured yet"


def test_adopt_seeds_the_cursor_so_history_is_not_replayed() -> None:
    """An adopted project can carry months of team log. With an empty cursor
    the first outbound poll would post ALL of it, one Slack message per entry.

    Create needs no such seeding -- a brand-new project has no history, and its
    first real milestone SHOULD be posted.
    """
    store = FakeStore()
    deps = _deps(store=store)

    studio_tools.dispatch(
        "adopt_project", {"project_id": "abovo"}, channel_id="C1",
        thread_ts="t1", confirmed_via="block_actions", deps=deps,
    )

    cursor = store.get_cursor("C-NEW")
    assert cursor, "adopt must seed the cursor at bind time"
