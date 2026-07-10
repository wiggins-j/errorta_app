"""F087-20 — accepted MVP is delivered to a real user-facing folder + run hint."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.deliverable import deliver, deliverable_dir, run_hint
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.workspace import CodingWorkspace


def _project(pid: str) -> tuple[LedgerStore, CodingWorkspace]:
    s = LedgerStore(pid)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    return s, ws


def test_export_master_is_clean_tracked_only(tmp_errorta_home: Path, tmp_path: Path) -> None:
    _s, ws = _project("exp1")
    ws.start_task_branch("t1")
    ws.write_file("calculator.py", "def add(a, b):\n    return a + b\n", task_id="t1")
    ws.merge_pr(ws.task_branch("t1"))

    dest = tmp_path / "out"
    ws.export(str(dest))
    assert (dest / "calculator.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (dest / ".git").exists()  # clean: no internal git dir


def test_deliver_new_target_exports_and_gives_run_hint(tmp_errorta_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_DELIVERABLES_DIR", str(tmp_path / "delivered"))
    s, ws = _project("del1")
    ws.start_task_branch("t1")
    ws.write_file("main.py", "if __name__ == '__main__':\n    print('hi')\n", task_id="t1")
    ws.merge_pr(ws.task_branch("t1"))

    d = deliver("del1", ws, target="new", repo_path=None)
    assert d["delivered_to"].endswith("/del1")
    assert (Path(d["delivered_to"]) / "main.py").exists()
    assert d["open_url"].startswith("file://")
    assert "python main.py" in d["run_hint"]


def test_run_hint_detects_python_and_node(tmp_path: Path) -> None:
    py = tmp_path / "py"; py.mkdir()
    (py / "calc.py").write_text("def add(a,b): return a+b\n")
    assert "python -c" in run_hint(py)

    node = tmp_path / "node"; node.mkdir()
    (node / "package.json").write_text("{}")
    assert "npm install" in run_hint(node)


def test_run_hint_index_html_is_platform_aware(tmp_path: Path, monkeypatch) -> None:
    site = tmp_path / "site"; site.mkdir()
    (site / "index.html").write_text("<html></html>")

    monkeypatch.setattr("sys.platform", "darwin")
    assert run_hint(site).startswith("open ")

    monkeypatch.setattr("sys.platform", "linux")
    assert run_hint(site).startswith("xdg-open ")

    monkeypatch.setattr("sys.platform", "win32")
    assert run_hint(site).startswith("start ")


def test_deliverable_dir_default_and_override(tmp_errorta_home: Path, monkeypatch) -> None:
    monkeypatch.delenv("ERRORTA_DELIVERABLES_DIR", raising=False)
    assert deliverable_dir("p").name == "p"
    assert deliverable_dir("p").parent.name == "Errorta Projects"
    monkeypatch.setenv("ERRORTA_DELIVERABLES_DIR", "/tmp/x")
    assert str(deliverable_dir("p")) == "/tmp/x/p"


def test_deliverable_dir_honors_explicit_delivery_root(tmp_errorta_home, monkeypatch) -> None:
    # An explicit delivery_root wins over the env override and the default.
    monkeypatch.setenv("ERRORTA_DELIVERABLES_DIR", "/tmp/env")
    assert str(deliverable_dir("p", "/tmp/chosen")) == "/tmp/chosen/p"
    # None falls back to the env override.
    assert str(deliverable_dir("p", None)) == "/tmp/env/p"


def test_deliver_uses_delivery_root_for_new_target(tmp_errorta_home, tmp_path) -> None:
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    s, ws = _project("delroot1")
    ws.start_task_branch("t1")
    ws.write_file("main.py", "print('hi')\n", task_id="t1")
    ws.merge_pr(ws.task_branch("t1"))

    d = deliver("delroot1", ws, target="new", repo_path=None,
                delivery_root=str(chosen))
    assert d["delivered_to"] == str(chosen / "delroot1")
    assert (chosen / "delroot1" / "main.py").exists()


# --- accept route: delivery-root + non-empty-destination guard --------------


def _make_acceptable_project(c, pid: str, delivery_root: str | None = None):
    """Create a greenfield project + drive it to an accept-ready state."""
    from errorta_council.coding.ledger import _atomic_write_json

    payload = {"project_id": pid, "north_star": "n",
               "definition_of_done": "d", "target": "new"}
    if delivery_root is not None:
        payload["delivery_root"] = delivery_root
    r = c.post("/coding/projects", json=payload)
    assert r.status_code == 200, r.text

    s = LedgerStore(pid)
    t = s.add_task(title="impl", role="dev")
    ws = CodingWorkspace(pid, s)
    ws.setup(target="new", repo_path=None)
    ws.start_task_branch(t.task_id)
    ws.write_file("calculator.py", "def add(a,b): return a+b\n", task_id=t.task_id)
    ws.merge_pr(ws.task_branch(t.task_id))
    s.update_task(t.task_id, state="done")
    head = ws.head()
    s.record_decision(title="r", context="c", choice="review_approved",
                      rationale="ok", extra={"reviewed_head": head})

    class _S:
        command_ids = ["unit"]
        results: list = []
        unknown_ids: list = []
        passed = True

    s.record_test_run(_S(), task_id=t.task_id, head=head)
    raw = s.get_project().to_dict()
    raw["status"] = "done"
    _atomic_write_json(s._project_path, raw)
    return s


def test_accept_delivers_to_custom_root(tmp_errorta_home, tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes

    root = tmp_path / "Projects"
    root.mkdir()
    app = FastAPI()
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    _make_acceptable_project(c, "accroot", delivery_root=str(root))

    r = c.post("/coding/projects/accroot/worktree/accept", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivered_to"] == str((root / "accroot").resolve())
    assert (root / "accroot" / "calculator.py").exists()


def test_accept_to_empty_destination_succeeds(tmp_errorta_home, tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes

    root = tmp_path / "Projects"
    root.mkdir()
    (root / "accempty").mkdir()  # pre-existing but EMPTY destination
    app = FastAPI()
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    _make_acceptable_project(c, "accempty", delivery_root=str(root))

    r = c.post("/coding/projects/accempty/worktree/accept", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert (root / "accempty" / "calculator.py").exists()


def test_accept_to_nonempty_destination_409(tmp_errorta_home, tmp_path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes

    root = tmp_path / "Projects"
    root.mkdir()
    dest = root / "accfull"
    dest.mkdir()
    (dest / "PREEXISTING.txt").write_text("do not clobber")  # non-empty
    app = FastAPI()
    app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    _make_acceptable_project(c, "accfull", delivery_root=str(root))

    r = c.post("/coding/projects/accfull/worktree/accept", json={"confirm": True})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "delivery_destination_not_empty"
    # The pre-existing file is untouched (no overwrite).
    assert (dest / "PREEXISTING.txt").read_text() == "do not clobber"
    assert not (dest / "calculator.py").exists()


# --- route: accept delivers + surfaces location -----------------------------


def test_accept_route_returns_delivery(tmp_errorta_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_DELIVERABLES_DIR", str(tmp_path / "out"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import _atomic_write_json

    app = FastAPI(); app.include_router(coding_routes.router)
    c = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})
    c.post("/coding/projects", json={"project_id": "acc", "north_star": "n",
           "definition_of_done": "d", "target": "new"})
    s = LedgerStore("acc")
    t = s.add_task(title="impl", role="dev")
    ws = CodingWorkspace("acc", s); ws.setup(target="new", repo_path=None)
    ws.start_task_branch(t.task_id)
    ws.write_file("calculator.py", "def add(a,b): return a+b\n", task_id=t.task_id)
    ws.merge_pr(ws.task_branch(t.task_id))
    # make the gate pass: task done, reviewed+tested on current head, DoD met
    s.update_task(t.task_id, state="done")
    head = ws.head()
    s.record_decision(title="r", context="c", choice="review_approved",
                      rationale="ok", extra={"reviewed_head": head})

    class _S:
        command_ids = ["unit"]; results: list = []; unknown_ids: list = []; passed = True
    s.record_test_run(_S(), task_id=t.task_id, head=head)
    raw = s.get_project().to_dict(); raw["status"] = "done"
    _atomic_write_json(s._project_path, raw)

    r = c.post("/coding/projects/acc/worktree/accept", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivered_to"].endswith("/acc")
    assert body["open_url"].startswith("file://")
    assert "python" in body["run_hint"].lower()
    assert (Path(body["delivered_to"]) / "calculator.py").exists()
