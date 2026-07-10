"""F141 WS-I — north_star_met_at / phase marker (drives Current Focus gating)."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore


def test_new_project_starts_in_north_star_phase(tmp_errorta_home: Path) -> None:
    store = LedgerStore("np")
    p = store.create_project(north_star="n", definition_of_done="d",
                             target="new", repo_path=None)
    assert p.north_star_met_at == ""
    assert p.phase == "north_star"
    assert p.to_dict()["north_star_met_at"] == ""


def test_mark_north_star_met_is_forward_only(tmp_errorta_home: Path) -> None:
    store = LedgerStore("np2")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    p1 = store.mark_north_star_met()
    assert p1.phase == "steering"
    assert p1.north_star_met_at
    # idempotent: a second call never moves the stamp
    p2 = store.mark_north_star_met()
    assert p2.north_star_met_at == p1.north_star_met_at


def test_imported_project_met_on_north_star_accept(tmp_errorta_home: Path) -> None:
    store = LedgerStore("ip")
    store.create_project(north_star="", definition_of_done="",
                         target="existing", repo_path="/tmp/x")
    assert store.get_project().phase == "north_star"
    p = store.promote_north_star("Ship it", "green tests")
    assert p.phase == "steering"
    assert p.north_star_met_at


def test_new_project_promote_does_not_auto_meet(tmp_errorta_home: Path) -> None:
    """A `new` project reaches steering only when it's `done`, not by editing its
    North Star — promote_north_star must NOT stamp it met."""
    store = LedgerStore("np3")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    p = store.promote_north_star("n2", "d2")
    assert p.phase == "north_star"
    assert p.north_star_met_at == ""


def test_reaching_done_meets_north_star(tmp_errorta_home: Path) -> None:
    """A `new` project crosses into steering when it reaches `done` (the North Star
    is met) — forward-only, so a later re-open to active stays in steering."""
    store = LedgerStore("np_done")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    assert store.get_project().phase == "north_star"
    p = store.set_project_status("done")
    assert p.phase == "steering"
    assert p.north_star_met_at
    stamp = p.north_star_met_at
    # Re-open (F146): status goes back to active, but steering is forward-only.
    p2 = store.set_project_status("active")
    assert p2.north_star_met_at == stamp
    assert p2.phase == "steering"


def test_project_out_surfaces_phase(tmp_errorta_home: Path) -> None:
    from errorta_app.routes.coding import _project_out
    store = LedgerStore("po")
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    out = _project_out(store)
    assert out["phase"] == "north_star"
    store.mark_north_star_met()
    assert _project_out(store)["phase"] == "steering"


# F141 WS-I backfill: projects predating the north_star_met_at marker (or an
# imported project whose North Star was accepted before it existed) must not be
# stuck hiding Current Focus.

def test_phase_backfill_existing_target_with_north_star(tmp_errorta_home: Path) -> None:
    from errorta_app.routes.coding import _project_out
    store = LedgerStore("bf_exist")
    # imported project, North Star set, but no north_star_met_at stamp (pre-F141)
    store.create_project(north_star="Ship it", definition_of_done="d",
                         target="existing", repo_path="/tmp/x")
    assert store.get_project().north_star_met_at == ""  # not stamped
    assert _project_out(store)["phase"] == "steering"


def test_phase_new_target_with_prs_but_not_done_stays_north_star(
        tmp_errorta_home: Path) -> None:
    # A new project mid-build (PRs merging) is NOT done, so the Current Focus panel
    # stays hidden — merely having build PRs no longer flips it to steering.
    from errorta_app.routes.coding import _project_out
    store = LedgerStore("bf_new")
    store.create_project(north_star="Build a game", definition_of_done="d",
                         target="new", repo_path=None)
    assert _project_out(store)["phase"] == "north_star"  # brand new, no PRs
    t = store.add_task(title="foundation", role="dev")
    store.record_pr(task_id=t.task_id, branch="feat/x", head="abc",
                    dev_member="m-dev")
    assert _project_out(store)["phase"] == "north_star"  # building, not done yet
    store.set_project_status("done")
    assert _project_out(store)["phase"] == "steering"  # met at done


def test_phase_backfill_done_project_without_stamp(tmp_errorta_home: Path) -> None:
    # A legacy `done` project predating the stamp still surfaces as steering.
    from errorta_app.routes.coding import _project_out
    store = LedgerStore("bf_done")
    store.create_project(north_star="Build a game", definition_of_done="d",
                         target="new", repo_path=None)
    # simulate a pre-stamp done project: status done, no north_star_met_at
    import json
    raw = json.loads((store._project_path).read_text())
    raw["status"] = "done"
    raw["north_star_met_at"] = ""
    store._project_path.write_text(json.dumps(raw))
    assert store.get_project().north_star_met_at == ""
    assert _project_out(store)["phase"] == "steering"


def test_phase_new_target_no_prs_stays_north_star(tmp_errorta_home: Path) -> None:
    from errorta_app.routes.coding import _project_out
    store = LedgerStore("bf_fresh")
    store.create_project(north_star="Build a game", definition_of_done="d",
                         target="new", repo_path=None)
    # planning-only (a task, but no PR yet) is still the brand-new phase
    store.add_task(title="plan", role="dev")
    assert _project_out(store)["phase"] == "north_star"
