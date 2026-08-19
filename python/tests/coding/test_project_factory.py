"""Slack studio manager Task 1 — the reusable engine-side helper that composes
a runnable coding project from a charter. Mirrors wizard_create() steps 1-5
(routes/coding.py) as plain library code, with no Tauri route and no Slack."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerError, LedgerStore
from errorta_council.coding.project_factory import create_project_from_charter

ROUTES = [{"route_id": "local.qwen", "family": "qwen", "provider_class": "local"}]

CHARTER = {
    "north_star": "Build a highscore-tracking game",
    "definition_of_done": "Players can submit and view highscores",
    "audience": "solo dev",
    "modality": "cli",
    "entrypoint": "python main.py",
    "scope_notes": "keep it small",
    "team_recipe": "fast_cheap",
    "autonomous": False,
}


def test_creates_runnable_project(tmp_errorta_home: Path):
    project = create_project_from_charter("hs-game", CHARTER, available_routes=ROUTES)

    store = LedgerStore("hs-game")
    proj = store.get_project()
    assert proj.north_star == CHARTER["north_star"]
    assert proj.definition_of_done == CHARTER["definition_of_done"]
    assert project.id == "hs-game"

    # workspace was set up
    from errorta_council.coding.workspace import CodingWorkspace
    ws = CodingWorkspace("hs-game", store)
    assert ws.root().is_dir()

    gov = GovernanceStore.for_ledger(store)
    artifacts = gov.list_artifacts(kind="brainstorm")
    assert len(artifacts) == 1
    assert artifacts[0].state == "approved"
    assert artifacts[0].body_json == CHARTER

    cfg = store.get_run_config()
    assert cfg["members"]
    assert proj  # from_dict round trip already checked north_star above


def test_run_setup_confirmed_when_team_assigned(tmp_errorta_home: Path):
    create_project_from_charter("hs-game2", CHARTER, available_routes=ROUTES)
    store = LedgerStore("hs-game2")
    assert store.get_project().run_setup_confirmed is True


def test_explicit_members_bypass_resolve_team(tmp_errorta_home: Path):
    members = [
        {"id": "pm-1", "role": "answerer", "enabled": True,
         "metadata": {"coding_role": "pm"}, "gateway_route_id": "local.qwen"},
    ]
    create_project_from_charter("hs-game3", CHARTER, available_routes=[], members=members)
    store = LedgerStore("hs-game3")
    assert store.get_run_config()["members"] == members
    assert store.get_project().run_setup_confirmed is True


def test_no_available_routes_creates_idle_project_without_crashing(tmp_errorta_home: Path):
    create_project_from_charter("hs-game4", CHARTER, available_routes=[])
    store = LedgerStore("hs-game4")
    proj = store.get_project()
    assert proj is not None
    cfg = store.get_run_config()
    assert not cfg.get("members")
    assert proj.run_setup_confirmed is False


def test_missing_north_star_raises_value_error(tmp_errorta_home: Path):
    bad = dict(CHARTER)
    del bad["north_star"]
    with pytest.raises(ValueError):
        create_project_from_charter("hs-game5", bad, available_routes=ROUTES)


def test_missing_team_recipe_raises_before_any_disk_write(tmp_errorta_home: Path):
    # team_recipe/autonomous aren't in REQUIRED_CHARTER_FIELDS but ARE
    # dereferenced later (after create_project/CodingWorkspace.setup write to
    # disk) — a missing one must be caught up front so no orphan is left.
    bad = dict(CHARTER)
    del bad["team_recipe"]
    with pytest.raises(ValueError):
        create_project_from_charter("hs-game6", bad, available_routes=ROUTES)
    with pytest.raises(Exception):
        LedgerStore("hs-game6").get_project()


def test_missing_autonomous_raises_before_any_disk_write(tmp_errorta_home: Path):
    bad = dict(CHARTER)
    del bad["autonomous"]
    with pytest.raises(ValueError):
        create_project_from_charter("hs-game7", bad, available_routes=ROUTES)
    with pytest.raises(Exception):
        LedgerStore("hs-game7").get_project()


def test_unsafe_project_id_raises(tmp_errorta_home: Path):
    with pytest.raises(LedgerError):
        create_project_from_charter("../x", CHARTER, available_routes=ROUTES)


# --------------------------------------------------------------------------
# Slice 5a Task 1 — the charter seeds the project's first Focus.
#
# Before this, a studio-created project had no active Focus and an empty
# work_request, so `next_goal.start_gate` refused its very first start and the
# documented create-then-start flow was unreachable from Slack.
# --------------------------------------------------------------------------


def test_create_from_charter_seeds_focus_from_north_star(tmp_errorta_home: Path):
    create_project_from_charter("seed-ns", CHARTER, available_routes=ROUTES)

    focuses = LedgerStore("seed-ns").active_focuses()
    assert len(focuses) == 1
    # Verbatim: a code-level copy is not an invention, whereas a generated
    # "first increment" would be.
    assert focuses[0].title == CHARTER["north_star"]
    assert focuses[0].origin == "studio_charter"


def test_create_from_charter_prefers_initial_goal(tmp_errorta_home: Path):
    charter = {**CHARTER, "initial_goal": "Ship the score submission form first"}
    create_project_from_charter("seed-goal", charter, available_routes=ROUTES)

    focuses = LedgerStore("seed-goal").active_focuses()
    assert len(focuses) == 1
    assert focuses[0].title == "Ship the score submission form first"


def test_created_project_is_not_refused_by_start_gate(tmp_errorta_home: Path):
    """The Gap A regression: this is the exact refusal seen live in Slack."""
    from errorta_council.coding import next_goal

    create_project_from_charter("seed-gate", CHARTER, available_routes=ROUTES)

    assert next_goal.start_gate(LedgerStore("seed-gate")) is None


def test_archived_focus_project_is_still_refused(tmp_errorta_home: Path):
    """Gate preservation: seeding must not disarm the gate permanently.

    Once the seeded Focus is archived the project HAS finished work and its
    charter may be stale -- precisely the case the gate exists for.
    """
    from errorta_council.coding import next_goal

    create_project_from_charter("seed-arch", CHARTER, available_routes=ROUTES)
    store = LedgerStore("seed-arch")
    store.update_focus(store.active_focuses()[0].id, status="archived")

    assert next_goal.start_gate(store) is not None
