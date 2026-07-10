"""F145 Slice 1 — live-state snapshot + reference-context assembly."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import pm_reference as pr
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore
from errorta_council.coding.workspace import CodingWorkspace


def _project(project_id: str) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    return store


def test_reference_text_loads_and_is_the_manual(tmp_errorta_home: Path):
    text = pr.load_reference_text()
    assert "PM Reference" in text
    assert "PM_REFERENCE_CONTRACT_START" in text  # the contract block is present


def test_pre_project_context_has_availability_and_defaults(tmp_errorta_home: Path):
    state = pr.build_live_state(None)
    assert "available_routes" in state and isinstance(state["available_routes"], list)
    assert state["project"] is None
    # defaults are advertised so the Wizard can reason about what it will set
    assert state["autonomy_defaults"]["checkpoint_cadence"] == "per_milestone"


def test_project_context_reflects_config_and_team(tmp_errorta_home: Path):
    store = _project("lsproj")
    # give it a runtime profile + a room member so the snapshot has something
    RuntimeProfileStore.for_ledger(store).upsert_profile(RuntimeProfile(
        profile_id="default", project_id="lsproj", kind="static",
        runtime_mode="managed_local", start=["python", "-m", "http.server"],
        health={"type": "none"}))
    store.set_run_config(room_id="team-1", members=[
        {"id": "d1", "metadata": {"coding_role": "dev"}, "model_mode": "single",
         "gateway_route_id": "local.qwen2.5-coder:7b"},
        {"id": "r1", "metadata": {"coding_role": "reviewer"}, "model_mode": "multi",
         "model_pool": ["local.a", "anthropic.b"]},
    ])

    state = pr.build_live_state("lsproj", store=store)
    proj = state["project"]
    assert proj is not None
    assert proj["autonomy"]["checkpoint_cadence"] == "per_milestone"
    assert proj["runtime"]["kind"] == "static"
    roles = {m["coding_role"] for m in proj["room"]["members"]}
    assert {"dev", "reviewer"} <= roles
    dev = next(m for m in proj["room"]["members"] if m["coding_role"] == "dev")
    assert dev["gateway_route_id"] == "local.qwen2.5-coder:7b"
    rev = next(m for m in proj["room"]["members"] if m["coding_role"] == "reviewer")
    assert rev["model_pool"] == ["local.a", "anthropic.b"]


def test_snapshot_never_leaks_secrets_or_prompts(tmp_errorta_home: Path):
    store = _project("secproj")
    # a member carrying a secret-shaped system prompt + metadata must not surface
    store.set_run_config(room_id="team-x", members=[
        {"id": "d1", "metadata": {"coding_role": "dev", "note": "sk-ant-SECRET"},
         "system_prompt": "sk-ant-DO-NOT-LEAK-abcdef", "model_mode": "single",
         "gateway_route_id": "local.q"},
    ])
    # a runtime profile carries the richer leak surface — argv, env-var names, a
    # health URL, a working dir — none of which may reach the snapshot.
    RuntimeProfileStore.for_ledger(store).upsert_profile(RuntimeProfile(
        profile_id="default", project_id="secproj", kind="cli",
        runtime_mode="managed_local", working_dir="/secret/work/dir",
        start=["python", "/secret/path/run.py", "--token=LEAKME"],
        env_required=["MY_SECRET_TOKEN"], health={"type": "http", "target": "http://internal.host/x"}))

    ctx = pr.build_pm_reference_context("secproj", store=store)
    for leak in ("sk-ant", "system_prompt", "/secret/path", "/secret/work",
                 "MY_SECRET_TOKEN", "internal.host", "LEAKME"):
        assert leak not in ctx, f"leaked: {leak}"
    # the manual + a live-state block are both present
    assert "LIVE STATE" in ctx and "available_routes" in ctx


def test_context_stable_content_is_reproducible(tmp_errorta_home: Path):
    # The manual + available-routes are the stable content; the config snapshot
    # legitimately carries live timestamps, so we assert the stable parts match
    # rather than byte-identity of the whole block.
    store = _project("detproj")
    a = pr.build_pm_reference_context("detproj", store=store)
    b = pr.build_pm_reference_context("detproj", store=store)
    manual_a = a.split("## LIVE STATE", 1)[0]
    manual_b = b.split("## LIVE STATE", 1)[0]
    assert manual_a == manual_b
    assert pr.list_available_routes() == pr.list_available_routes()
