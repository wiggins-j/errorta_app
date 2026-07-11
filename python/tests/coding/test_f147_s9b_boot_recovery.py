"""F147 S9b — owner-aware BOOT recovery (closes the S9a HIGH).

S9a made the ``GET /run``/start paths owner-pid-aware, but the *boot* recovery
scan (``scan_and_recover`` → ``recover_orphaned_run``) stayed owner-blind: a
second sidecar's boot could reconcile a run that is live in ANOTHER sidecar to
``interrupted`` (§4.2 corruption). S9b threads an ``owner_peer_fn`` seam through
the boot scan, backed by the pure ``locks.owner_is_live_peer_sidecar`` predicate,
which confirms a live *advertised* peer sidecar (advert pid + /healthz cross-
check) so recovery stands down — while staying fail-OPEN so a genuine orphan
(dead owner, or a REUSED pid that isn't the advertised sidecar) is still cleared.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import locks
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.run_recovery import scan_and_recover

# --------------------------------------------------------------------------- #
# owner_is_live_peer_sidecar — the pure predicate matrix.
# --------------------------------------------------------------------------- #

_ADVERT = {"pid": 4321, "port": 5555}


def _healthz_ok(port: int) -> dict:
    return {"pid": 4321, "service": "errorta-sidecar"}


def _running(owner_pid: int) -> dict:
    return {"status": "running", "owner_pid": owner_pid}


def test_peer_confirmed_when_alive_advertised_and_healthz_pid_matches() -> None:
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )
        is True
    )


def test_not_peer_when_owner_dead() -> None:
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: False,  # owner_pid dead → orphan
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )
        is False
    )


def test_not_peer_when_no_advertisement() -> None:
    # owner_pid alive (a REUSED pid) but nothing advertised → orphan → recover.
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=None,
            healthz_fn=_healthz_ok,
        )
        is False
    )


def test_not_peer_when_advert_pid_mismatches_owner() -> None:
    # A live advert names a DIFFERENT sidecar than owner_pid → owner is a reused
    # pid, not the advertised sidecar → orphan.
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: True,
            advert={"pid": 7777, "port": 5555},
            healthz_fn=_healthz_ok,
        )
        is False
    )


def test_not_peer_when_healthz_reports_different_pid() -> None:
    # advert says pid 4321, but /healthz on that port answers pid 8888 (the
    # advertised port was reused by an unrelated sidecar) → not our peer.
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=lambda port: {"pid": 8888},
        )
        is False
    )


def test_not_peer_when_healthz_probe_fails() -> None:
    # Probe error/timeout → fail OPEN toward recovery (orphan safety-net).
    def boom(port: int) -> dict:
        raise RuntimeError("healthz timed out")

    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=boom,
        )
        is False
    )


def test_not_peer_for_our_own_pid_or_non_running() -> None:
    assert (
        locks.owner_is_live_peer_sidecar(
            _running(4321),
            my_pid=4321,  # our own pid
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )
        is False
    )
    assert (
        locks.owner_is_live_peer_sidecar(
            {"status": "idle", "owner_pid": 4321},
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )
        is False
    )


# --------------------------------------------------------------------------- #
# scan_and_recover — the owner-aware boot matrix (real LedgerStore state).
# --------------------------------------------------------------------------- #

def _running_project(
    tmp_errorta_home: Path, project_id: str, owner_pid: int
) -> LedgerStore:
    store = LedgerStore(project_id)
    store.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None
    )
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running", owner_pid=owner_pid)
    return store


def test_boot_recovery_stands_down_for_live_peer_owner(
    tmp_errorta_home: Path,
) -> None:
    _running_project(tmp_errorta_home, "ppeer", owner_pid=4321)

    def owner_peer_fn(state: dict) -> bool:
        # Confirmed live advertised peer for this exact owner_pid.
        return locks.owner_is_live_peer_sidecar(
            state,
            my_pid=99,
            alive_fn=lambda pid: True,
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )

    summary = scan_and_recover(owner_peer_fn=owner_peer_fn)

    assert summary.interrupted_projects == []
    reloaded = LedgerStore("ppeer").get_run_state()
    assert reloaded["status"] == "running"
    assert LedgerStore("ppeer").list_tasks()[0].state == "doing"


def test_boot_recovery_clears_orphan_with_dead_owner(
    tmp_errorta_home: Path,
) -> None:
    _running_project(tmp_errorta_home, "pdead", owner_pid=4321)

    def owner_peer_fn(state: dict) -> bool:
        return locks.owner_is_live_peer_sidecar(
            state,
            my_pid=99,
            alive_fn=lambda pid: False,  # owner is dead → genuine orphan
            advert=dict(_ADVERT),
            healthz_fn=_healthz_ok,
        )

    summary = scan_and_recover(owner_peer_fn=owner_peer_fn)

    assert "pdead" in summary.interrupted_projects
    assert LedgerStore("pdead").get_run_state()["status"] == "interrupted"
    assert LedgerStore("pdead").list_tasks()[0].state == "todo"


def test_boot_recovery_clears_orphan_when_reused_pid_is_not_a_sidecar(
    tmp_errorta_home: Path,
) -> None:
    # The pid-reuse case: owner_pid is ALIVE (reassigned to an unrelated process)
    # but it is not the advertised sidecar — /healthz on the advertised port
    # doesn't report that pid. Recovery must still clear this orphan.
    _running_project(tmp_errorta_home, "preuse", owner_pid=4321)

    def owner_peer_fn(state: dict) -> bool:
        return locks.owner_is_live_peer_sidecar(
            state,
            my_pid=99,
            alive_fn=lambda pid: True,  # reused pid → alive
            advert=dict(_ADVERT),  # advert pid matches (stale)…
            healthz_fn=lambda port: {"pid": 8888},  # …but healthz is a different sidecar
        )

    summary = scan_and_recover(owner_peer_fn=owner_peer_fn)

    assert "preuse" in summary.interrupted_projects
    assert LedgerStore("preuse").get_run_state()["status"] == "interrupted"


def test_boot_recovery_owner_blind_when_no_seam(tmp_errorta_home: Path) -> None:
    # owner_peer_fn=None preserves the exact pre-S9b behavior: an orphaned
    # running run (no live worker in this process) is reconciled.
    _running_project(tmp_errorta_home, "pblind", owner_pid=4321)

    summary = scan_and_recover()

    assert "pblind" in summary.interrupted_projects


def test_boot_recovery_fails_open_when_peer_fn_raises(
    tmp_errorta_home: Path,
) -> None:
    # A peer check that raises must NOT wedge a real orphan — recovery proceeds.
    _running_project(tmp_errorta_home, "praise", owner_pid=4321)

    def boom(state: dict) -> bool:
        raise RuntimeError("peer check exploded")

    summary = scan_and_recover(owner_peer_fn=boom)

    assert "praise" in summary.interrupted_projects
