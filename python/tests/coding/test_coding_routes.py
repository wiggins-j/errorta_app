from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_errorta_home: Path) -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers={"x-errorta-origin": "tauri-ui"})


def test_resume_refuses_on_workspace_integrity_failure(tmp_errorta_home: Path) -> None:
    # F087-15 M2: resume against a missing/mismatched worktree -> 409, never a
    # silent reset.
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pintegrity", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("pintegrity")
    # interrupted run with a persisted head that no live worktree matches
    store.set_run_state(status="interrupted", workspace_head="deadbeefdeadbeef")
    members = [{"id": "m", "enabled": True, "gateway_route_id": "fake.local.deterministic",
                "provider_kind": "local"}]
    r = c.post("/coding/projects/pintegrity/run/resume", json={"members": members})
    assert r.status_code == 409
    assert r.json()["detail"] == "workspace_integrity_failed"


def test_fingerprint_match_ignores_volatile_primary_pointer() -> None:
    # Resume must NOT fail just because the shared root worktree is checked out on
    # a different branch than at interrupt-time (the runner moves that pointer; a
    # reconcile leaves it on master). Only the work-bearing parts matter.
    from errorta_app.routes.coding import _fingerprint_matches
    base = {
        "format": "coding-workspace-fingerprint.v1",
        "branches": {"master": "aaa", "task-t-1": "bbb"},
        "worktrees": {"t-1": {"branch": "task-t-1", "exists": True, "head": "bbb"}},
    }
    interrupted = {**base, "primary": {"branch": "task-t-1", "head": "bbb"}}
    after_reconcile = {**base, "primary": {"branch": "master", "head": "aaa"}}
    assert _fingerprint_matches(interrupted, after_reconcile) is True
    # But a genuinely changed task branch head (lost/rewritten work) still fails.
    tampered = {**after_reconcile, "branches": {"master": "aaa", "task-t-1": "XXX"}}
    assert _fingerprint_matches(interrupted, tampered) is False


def test_require_sandbox_setting_route(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "psbx", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    assert c.get("/coding/projects/psbx/test-settings").json()["require_sandbox"] is False
    r = c.put("/coding/projects/psbx/test-settings", json={"require_sandbox": True})
    assert r.status_code == 200
    assert LedgerStore("psbx").get_require_sandbox() is True


def test_create_then_get_project(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    r = c.post("/coding/projects", json={"project_id": "p1", "north_star": "Build X",
               "definition_of_done": "tests pass", "target": "new"})
    assert r.status_code == 200, r.text
    g = c.get("/coding/projects/p1")
    assert g.status_code == 200 and g.json()["project"]["north_star"] == "Build X"


def test_get_missing_project_404(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    assert c.get("/coding/projects/ghost").status_code == 404


def test_add_task_and_orientation(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "p2", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    r = c.post("/coding/projects/p2/tasks", json={"title": "impl", "role": "dev"})
    assert r.status_code == 200 and r.json()["task"]["role"] == "dev"
    o = c.get("/coding/projects/p2/orientation?token_budget=5000")
    assert o.status_code == 200 and o.json()["north_star"] == "n"


def test_invalid_role_400(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "p3", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    r = c.post("/coding/projects/p3/tasks", json={"title": "x", "role": "wizard"})
    assert r.status_code == 400


def test_edit_north_star(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "p4", "north_star": "old",
           "definition_of_done": "d", "target": "new"})
    r = c.put("/coding/projects/p4/north-star", json={"north_star": "new goal"})
    assert r.status_code == 200 and r.json()["project"]["north_star"] == "new goal"
    assert r.json()["project"]["revision"] == 2


def test_autonomy_policy_get_put(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pa", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    g = c.get("/coding/projects/pa/autonomy")
    assert g.status_code == 200 and "checkpoint_cadence" in g.json()["policy"]
    r = c.put("/coding/projects/pa/autonomy",
              json={"checkpoint_cadence": "every_n_tasks", "checkpoint_n": 4})
    assert r.status_code == 200 and r.json()["policy"]["checkpoint_n"] == 4
    assert c.get("/coding/projects/pa/autonomy").json()["policy"]["checkpoint_cadence"] == "every_n_tasks"


def test_guardrail_get_put_defaults_on(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pg", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    assert c.get("/coding/projects/pg/guardrail").json()["enabled"] is True
    assert c.put("/coding/projects/pg/guardrail", json={"enabled": False}).json()["enabled"] is False
    assert c.get("/coding/projects/pg/guardrail").json()["enabled"] is False


def test_worktree_preview_404_or_409_without_setup(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pw", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    # no worktree created yet -> 409
    assert c.get("/coding/projects/pw/worktree").status_code == 409
    # accept requires confirm:true
    assert c.post("/coding/projects/pw/worktree/accept", json={}).status_code == 400


def test_artifacts_route(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "par", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    assert c.get("/coding/projects/par/artifacts").json()["artifacts"] == []


def test_tool_events_route(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pte", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    LedgerStore("pte").record_tool_event(
        turn_id="turn-1", task_id="t1", member_id="m-dev", role="dev",
        tool="code_write", status="succeeded",
        intent={"path": "a.py"}, result={"path": "a.py"},
    )
    got = c.get("/coding/projects/pte/tool-events").json()["tool_events"]
    assert got[0]["tool"] == "code_write" and got[0]["status"] == "succeeded"


def test_list_projects(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pl1", "north_star": "Goal one",
           "definition_of_done": "d", "target": "new"})
    c.post("/coding/projects", json={"project_id": "pl2", "north_star": "Goal two",
           "definition_of_done": "d", "target": "new"})
    got = c.get("/coding/projects").json()["projects"]
    ids = {p["id"] for p in got}
    assert {"pl1", "pl2"} <= ids


def test_delete_project_removes_ledger_and_owned_workspace(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pdel", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("pdel")
    ws = CodingWorkspace("pdel", store)
    workspace_root = ws.setup(target="new", repo_path=None)
    assert store.dir.exists()
    assert workspace_root.exists()

    r = c.delete("/coding/projects/pdel")

    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "project_id": "pdel"}
    assert c.get("/coding/projects/pdel").status_code == 404
    assert not store.dir.exists()
    assert not workspace_root.exists()
    ids = {p["id"] for p in c.get("/coding/projects").json()["projects"]}
    assert "pdel" not in ids


def test_delete_project_reaps_running_runtime(tmp_errorta_home: Path) -> None:
    # F157: deleting a project whose managed-local server is still running must
    # stop that server (no leaked process/port) AND not 500 on the worktree
    # removal. Reproduces the live failure (a `next dev` outliving delete).
    import os
    import time

    from errorta_council.coding import runtime_process as rp
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.runtime import (
        RuntimeProfileStore,
        validate_profile,
    )
    from errorta_council.coding.workspace import CodingWorkspace

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "preap", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("preap")
    ws = CodingWorkspace("preap", store)
    workspace_root = ws.setup(target="new", repo_path=None)
    rstore = RuntimeProfileStore.for_ledger(store)
    rstore.upsert_profile(validate_profile(
        {"kind": "web", "runtime_mode": "managed_local",
         "start": ["python", "-c", "import time; print('serving'); time.sleep(60)"],
         "sandbox": "none"}, profile_id="default", project_id="preap"))

    mgr = rp.RuntimeProcessManager.for_project("preap")
    sess = mgr.start("default")
    try:
        time.sleep(0.4)
        sess = rstore.get_session(sess.session_id)
        assert sess.pgid is not None
        assert rp._pgid_alive(sess.pgid) is True, "server should be running"

        r = c.delete("/coding/projects/preap")

        assert r.status_code == 200, r.text
        assert not workspace_root.exists()
        # The server's process group is gone — no leak past delete.
        assert rp._pgid_alive(sess.pgid) is False, "delete must reap the server"
    finally:
        if sess.pgid is not None:
            try:
                os.killpg(sess.pgid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass


def test_delete_project_refuses_live_run(tmp_errorta_home: Path) -> None:
    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import LedgerStore

    class _AliveThread:
        def is_alive(self) -> bool:
            return True

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pbusy", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    coding_routes._RUNS["pbusy"] = {"thread": _AliveThread()}
    try:
        r = c.delete("/coding/projects/pbusy")
    finally:
        coding_routes._RUNS.pop("pbusy", None)

    assert r.status_code == 409
    assert r.json()["detail"] == "project run is still active"
    assert LedgerStore("pbusy").get_project().id == "pbusy"


def _confirm_run_setup(pid: str) -> None:
    # F121: clear the readiness gate so these run-mechanics tests reach the start
    # path (a fresh project's first Start otherwise opens the gate).
    from errorta_app.routes.coding import _set_run_setup_confirmed
    from errorta_council.coding.ledger import LedgerStore
    _set_run_setup_confirmed(LedgerStore(pid), True)


def test_run_route_smoke_with_fake_provider(tmp_errorta_home: Path) -> None:
    import time
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "prun", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("prun")
    members = [
        {"id": "m-pm", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "pm"}},
        {"id": "m-dev", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "dev"}},
    ]
    r = c.post("/coding/projects/prun/run", json={"members": members})
    assert r.status_code == 200 and r.json()["started"] is True
    # poll until the background run finishes (fake provider -> no parseable tasks
    # -> stops fast on no_progress); must never error out.
    for _ in range(50):
        st = c.get("/coding/projects/prun/run").json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.2)
    st = c.get("/coding/projects/prun/run").json()
    # Wiring smoke: the background run completed and the status endpoint surfaced
    # a structured result (a stop_reason, or a caught error from the gateway —
    # the full pipeline correctness is proven in test_coding_runner).
    assert st["result"] is not None
    assert "stop_reason" in st["result"] or "error" in st["result"]


def test_worker_baseexception_records_failure_not_silent_strand(
    tmp_errorta_home: Path, monkeypatch
) -> None:
    # Regression: a SystemExit-class error raised deep in a member turn used to
    # escape `except Exception`, silently kill the daemon worker thread, and leave
    # run_state stuck at "running" with no live worker — so every status poll
    # re-flagged the run "interrupted (resumable)" forever. The worker must catch
    # BaseException and record a terminal failure with the real message instead.
    import time

    from errorta_council.coding import runner as runner_mod

    def _boom(self, *a, **k):  # noqa: ANN001
        raise SystemExit("simulated SystemExit from a member turn")

    monkeypatch.setattr(runner_mod.CodingRunner, "run", _boom)

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pboom", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pboom")
    members = [
        {"id": "m-pm", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "pm"}},
    ]
    assert c.post("/coding/projects/pboom/run", json={"members": members}).status_code == 200
    st = {}
    for _ in range(50):
        st = c.get("/coding/projects/pboom/run").json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.1)
    # Terminal FAILED with the real reason surfaced — never stuck "running", never
    # an endless "interrupted".
    assert st["running"] is False
    assert st["state"]["status"] == "failed"
    assert "SystemExit" in (st["state"].get("last_error") or "")
    assert st["result"] is not None and "error" in st["result"]


def test_worker_transient_gateway_error_records_actionable_failure(
    tmp_errorta_home: Path, monkeypatch
) -> None:
    """A wire/decompression escape must keep the run restartable and hide the
    cryptic raw zlib message from the persisted user-facing state."""
    import time
    import zlib

    from errorta_council.coding import runner as runner_mod

    def _boom(self, *a, **k):  # noqa: ANN001
        raise zlib.error(
            "Error -3 while decompressing data: incorrect header check"
        )

    monkeypatch.setattr(runner_mod.CodingRunner, "run", _boom)

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pdecode", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pdecode")
    members = [
        {"id": "m-pm", "enabled": True,
         "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local", "metadata": {"coding_role": "pm"}},
    ]
    assert c.post(
        "/coding/projects/pdecode/run", json={"members": members}
    ).status_code == 200

    st = {}
    for _ in range(50):
        st = c.get("/coding/projects/pdecode/run").json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.1)

    state = st["state"]
    assert st["running"] is False
    assert state["status"] == "failed"
    assert state["last_error"] == (
        "transient model-backend error (network/decompression) — retry the run"
    )
    assert "incorrect header check" not in state["last_error"]
    assert state["recoverable"] is True
    assert state["can_resume"] is False


def test_run_route_404_and_400(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    assert c.post("/coding/projects/ghost/run", json={"members": [{"id": "x"}]}).status_code == 404
    c.post("/coding/projects/pe/run", json={})  # create-less? no project -> 404 first
    c.post("/coding/projects", json={"project_id": "pe", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pe")  # F121: past the gate, so the no-members 400 is reached
    assert c.post("/coding/projects/pe/run", json={}).status_code == 400  # no members


def test_run_auto_assigns_coding_roles_from_plain_members(tmp_errorta_home: Path) -> None:
    # A room/member set with NO coding_role still yields a workable team
    # (roles assigned by position) instead of a 400/empty-team.
    import time
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pauto", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    _confirm_run_setup("pauto")
    members = [
        {"id": f"m{i}", "enabled": True, "gateway_route_id": "fake.local.deterministic",
         "provider_kind": "local"} for i in range(4)
    ]
    r = c.post("/coding/projects/pauto/run", json={"members": members})
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(50):
        st = c.get("/coding/projects/pauto/run").json()
        if not st["running"] and st["result"] is not None:
            break
        time.sleep(0.2)
    st = c.get("/coding/projects/pauto/run").json()
    # it ran (didn't 400 on no-PM); result populated
    assert st["result"] is not None


def test_create_project_rejects_bad_id(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    for bad in ["../x", "a/b", "..", "/abs"]:
        r = c.post("/coding/projects", json={"project_id": bad, "north_star": "n",
                   "definition_of_done": "d", "target": "new"})
        assert r.status_code == 422, f"{bad} -> {r.status_code}"
    # no escaped file created
    import os
    assert not os.path.exists(str(tmp_errorta_home.parent / "x" / "project.json"))


def test_mutating_routes_require_tauri_origin(tmp_errorta_home: Path) -> None:
    from fastapi.testclient import TestClient

    from errorta_app.server import app
    noorigin = TestClient(app)  # NO x-errorta-origin header
    # create is a mutation -> 403 without the origin header
    r = noorigin.post("/coding/projects", json={"project_id": "po", "north_star": "n",
                      "definition_of_done": "d", "target": "new"})
    assert r.status_code == 403, r.text
    # reads stay open
    assert noorigin.get("/coding/projects").status_code == 200


def test_interject_route(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pi", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    r = c.post("/coding/projects/pi/interject", json={"message": "go faster"})
    assert r.status_code == 200 and r.json()["ok"] is True
    reply = r.json()["interjection"]["pm_reply"]
    assert reply["kind"] == "queued_directive"
    assert reply["progress"]["total"] == 0
    assert c.post("/coding/projects/pi/interject", json={"message": "  "}).status_code == 400
    # recorded as an interjection (not a backlog task)
    assert c.get("/coding/projects/pi/backlog").json()["tasks"] == []


def test_interject_route_returns_pm_progress_reply(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore

    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pi-progress", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    done = c.post("/coding/projects/pi-progress/tasks", json={"title": "scaffold", "role": "dev"}).json()["task"]
    doing = c.post("/coding/projects/pi-progress/tasks", json={"title": "implement UI", "role": "dev"}).json()["task"]
    todo = c.post("/coding/projects/pi-progress/tasks", json={"title": "review", "role": "reviewer"}).json()["task"]
    blocked = c.post("/coding/projects/pi-progress/tasks", json={"title": "ship", "role": "tester"}).json()["task"]
    c.patch(f"/coding/projects/pi-progress/tasks/{done['task_id']}", json={"state": "done"})
    c.patch(f"/coding/projects/pi-progress/tasks/{doing['task_id']}", json={"state": "doing"})
    c.patch(f"/coding/projects/pi-progress/tasks/{blocked['task_id']}", json={"state": "blocked"})

    r = c.post("/coding/projects/pi-progress/interject",
               json={"message": "How close are we to being done?"})

    assert r.status_code == 200
    interjection = r.json()["interjection"]
    reply = interjection["pm_reply"]
    assert reply["role"] == "pm"
    assert reply["kind"] == "progress_summary"
    assert reply["progress"] == {
        "total": 4, "done": 1, "doing": 1, "todo": 1, "blocked": 1, "percent": 25,
    }
    assert "We're 25% done by task count" in reply["message"]
    assert "Doing: implement UI." in reply["message"]
    assert "Blocked: ship." in reply["message"]
    assert "Next: review." in reply["message"]
    assert set(reply["source_ids"]) == {
        done["task_id"], doing["task_id"], todo["task_id"], blocked["task_id"],
    }
    recorded = LedgerStore("pi-progress").list_unconsumed_interjections()[0]
    assert recorded["pm_reply"]["progress"]["percent"] == 25


def test_run_state_persists_and_reconciles(tmp_errorta_home: Path) -> None:
    # F087-07-F: run lifecycle is durable. A persisted 'stopped' is reported
    # even after a "restart" (cleared _RUNS); a persisted 'running' with no live
    # thread reconciles to 'interrupted'; cancel persists to the ledger.
    from errorta_app.routes.coding import _RUNS
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "prs", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("prs")

    # a finished run, then simulate a sidecar restart
    store.set_run_state(status="stopped", stop_reason="definition_of_done",
                        counters={"iterations": 7})
    _RUNS.clear()
    st = c.get("/coding/projects/prs/run").json()
    assert st["running"] is False
    assert st["result"]["stop_reason"] == "definition_of_done"
    assert st["result"]["iterations"] == 7

    # an orphaned 'running' (thread gone) -> interrupted
    store.set_run_state(status="running")
    _RUNS.clear()
    st2 = c.get("/coding/projects/prs/run").json()
    assert st2["state"]["status"] == "interrupted" and st2["running"] is False
    assert st2["recoverable"] is True and st2["can_resume"] is True
    assert st2["result"]["stop_reason"] == "interrupted"

    # cancel persists to the ledger
    c.post("/coding/projects/prs/run/cancel")
    assert LedgerStore("prs").get_run_state()["cancel_requested"] is True


def test_run_recovery_requeues_doing_task_on_status_read(tmp_errorta_home: Path) -> None:
    from errorta_app.routes.coding import _RUNS
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "preq", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("preq")
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running")
    _RUNS.clear()

    st = c.get("/coding/projects/preq/run").json()

    assert st["state"]["status"] == "interrupted"
    assert st["state"]["requeued_task_ids"] == [task.task_id]
    assert LedgerStore("preq").list_tasks()[0].state == "todo"
    assert LedgerStore("preq").list_tasks()[0].assignee_member_id is None
    assert LedgerStore("preq").list_decisions()[-1]["choice"] == "run_interrupted"


def test_resume_requires_interrupted_run(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "presume0", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    members = [{"id": "m", "enabled": True, "gateway_route_id": "fake.local.deterministic",
                "provider_kind": "local"}]

    r = c.post("/coding/projects/presume0/run/resume", json={"members": members})

    assert r.status_code == 409


def test_resume_interrupted_run_starts_fresh_worker(tmp_errorta_home: Path) -> None:
    from errorta_app.routes.coding import _RUNS
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "presume", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    store = LedgerStore("presume")
    store.set_run_state(status="running")
    _RUNS.clear()
    assert c.get("/coding/projects/presume/run").json()["state"]["status"] == "interrupted"
    members = [{"id": "m", "enabled": True, "gateway_route_id": "fake.local.deterministic",
                "provider_kind": "local"}]

    r = c.post("/coding/projects/presume/run/resume", json={"members": members})

    assert r.status_code == 200, r.text
    assert r.json()["started"] is True and r.json()["resumed"] is True
    state = LedgerStore("presume").get_run_state()
    assert state["resumed_from_status"] == "interrupted"
    assert state["cancel_requested"] is False


def test_lifespan_runs_coding_recovery_at_startup(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore("pboot")
    store.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    task = store.add_task(title="impl", role="dev")
    store.update_task(task.task_id, state="doing", assignee_member_id="m-dev")
    store.set_run_state(status="running")

    with TestClient(app, headers={"x-errorta-origin": "tauri-ui"}):
        pass

    assert LedgerStore("pboot").get_run_state()["status"] == "interrupted"
    assert LedgerStore("pboot").list_tasks()[0].state == "todo"
    assert "pboot" in app.state.coding_recovery.interrupted_projects


def test_test_commands_roundtrip_and_validation(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "ptc", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    # empty by default
    assert c.get("/coding/projects/ptc/test-commands").json()["commands"] == {}
    # valid registry round-trips
    cmds = {"unit": {"argv": ["python", "-m", "pytest", "-q"], "cwd": ".",
                     "timeout_seconds": 60}}
    r = c.put("/coding/projects/ptc/test-commands", json={"commands": cmds})
    assert r.status_code == 200, r.text
    assert c.get("/coding/projects/ptc/test-commands").json()["commands"]["unit"]["argv"][0] == "python"
    # malformed registry -> 422
    bad = c.put("/coding/projects/ptc/test-commands",
                json={"commands": {"unit": {"argv": []}}})
    assert bad.status_code == 422, bad.text


def test_test_commands_require_tauri_origin(tmp_errorta_home: Path) -> None:
    from errorta_app.server import app
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "pto", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    noorigin = TestClient(app)  # NO x-errorta-origin header
    r = noorigin.put("/coding/projects/pto/test-commands",
                     json={"commands": {"u": {"argv": ["x"]}}})
    assert r.status_code == 403
    # reads are open
    assert noorigin.get("/coding/projects/pto/test-runs").status_code == 200


def test_test_runs_listing_shape(tmp_errorta_home: Path) -> None:
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "ptr", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    assert c.get("/coding/projects/ptr/test-runs").json() == {"runs": []}


def test_prs_route(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    c = _client(tmp_errorta_home)
    c.post("/coding/projects", json={"project_id": "prr", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    assert c.get("/coding/projects/prr/prs").json()["prs"] == []
    LedgerStore("prr").record_pr(task_id="t1", branch="task-t1", head="h",
                                 dev_member="m-dev")
    prs = c.get("/coding/projects/prr/prs").json()["prs"]
    assert len(prs) == 1 and prs[0]["branch"] == "task-t1" and prs[0]["status"] == "open"
