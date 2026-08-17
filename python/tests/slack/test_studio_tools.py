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
    def __init__(self) -> None:
        self.staged: list[tuple[str, dict[str, Any], str]] = []
        self.bound: list[tuple[str, str]] = []
        self._next_cid = 0

    def stage_confirmation(self, verb: str, args: dict[str, Any], thread_ts: str) -> str:
        self.staged.append((verb, args, thread_ts))
        self._next_cid += 1
        return f"cid-{self._next_cid}"

    def bind_channel(self, channel_id: str, project_id: str) -> None:
        self.bound.append((channel_id, project_id))


class FakeProject:
    def __init__(self, project_id: str) -> None:
        self.id = project_id


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
                    available_routes: Any = None) -> FakeProject:
        calls.append((project_id, charter))
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
    assert store.staged == [("create_project", _charter(), "t1")]


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
                            available_routes: Any = None) -> Any:
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
