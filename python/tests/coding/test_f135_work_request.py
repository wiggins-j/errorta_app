"""F135 S3 — the current-focus work_request directive."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_TAURI = {"x-errorta-origin": "tauri-ui"}


def _client() -> TestClient:
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    return TestClient(app, headers=_TAURI)


def _project(project_id: str = "proj"):
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    store.create_project(north_star="ns", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def test_set_work_request_persists_and_round_trips(tmp_errorta_home: Path) -> None:
    _project()
    client = _client()
    r = client.put("/coding/projects/proj/work-request",
                   json={"work_request": "add rate limiting"})
    assert r.status_code == 200
    assert r.json()["project"]["work_request"] == "add rate limiting"
    # persisted
    from errorta_council.coding.ledger import LedgerStore
    assert LedgerStore("proj").get_project().work_request == "add rate limiting"


def test_work_request_is_capped(tmp_errorta_home: Path) -> None:
    store = _project()
    proj = store.set_work_request("x" * 50_000)
    assert len(proj.work_request) == 20_000


def test_orientation_packet_carries_and_pins_work_request(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.orientation import build_orientation_packet
    store = _project()
    store.set_work_request("focus on the parser")
    pkt = build_orientation_packet(LedgerStore("proj"), token_budget=2000)
    assert pkt.work_request == "focus on the parser"
    assert pkt.to_dict()["work_request"] == "focus on the parser"

    # Pinned: even under a tiny budget (which trims decisions/artifacts/tasks),
    # work_request survives.
    for i in range(20):
        store.record_decision(title=f"d{i}", context="c", choice="x",
                              rationale="r" * 200)
    tight = build_orientation_packet(LedgerStore("proj"), token_budget=50)
    assert tight.to_dict()["work_request"] == "focus on the parser"


def test_first_pm_turn_prompt_contains_work_request(tmp_errorta_home: Path) -> None:
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.runner import _pm_prompt
    store = _project()
    store.set_work_request("wire the webhook")
    prompt = _pm_prompt(LedgerStore("proj"))
    assert "CURRENT FOCUS" in prompt
    assert "wire the webhook" in prompt


def test_live_edit_supersedes_prior_work_request(tmp_errorta_home: Path) -> None:
    store = _project()
    store.supersede_work_request_interjection("first focus")
    store.supersede_work_request_interjection("second focus")
    pending = store.list_unconsumed_interjections()
    wr = [p for p in pending if p.get("kind") == "work_request"]
    assert len(wr) == 1
    assert wr[0]["message"] == "second focus"


def test_live_edit_keeps_unrelated_interjections(tmp_errorta_home: Path) -> None:
    store = _project()
    store.record_interjection("please also update the docs")  # unrelated
    store.supersede_work_request_interjection("focus A")
    store.supersede_work_request_interjection("focus B")
    messages = [p["message"] for p in store.list_unconsumed_interjections()]
    assert "please also update the docs" in messages
    assert "focus B" in messages
    assert "focus A" not in messages


def test_supersede_does_not_lose_concurrent_appends(tmp_errorta_home: Path) -> None:
    """F135 review #1 regression: a live PM appending interjections must not be
    clobbered by a concurrent PUT /work-request rewrite."""
    import threading

    store = _project()
    appended = 200

    def appender() -> None:
        for i in range(appended):
            store.record_interjection(f"live-{i}")

    def superseder() -> None:
        for i in range(appended):
            store.supersede_work_request_interjection(f"focus-{i}")

    t1 = threading.Thread(target=appender)
    t2 = threading.Thread(target=superseder)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    all_recs = store.list_unconsumed_interjections()
    live = [r for r in all_recs if str(r.get("message", "")).startswith("live-")]
    assert len(live) == appended, f"lost {appended - len(live)} appended records"
    # exactly one surviving work_request (the newest), never duplicated/corrupted
    wr = [r for r in all_recs if r.get("kind") == "work_request"]
    assert len(wr) == 1


def test_work_request_requires_tauri_origin(tmp_errorta_home: Path) -> None:
    _project()
    from errorta_app.routes import coding as coding_routes
    app = FastAPI()
    app.include_router(coding_routes.router)
    bare = TestClient(app)  # no origin header
    r = bare.put("/coding/projects/proj/work-request", json={"work_request": "x"})
    assert r.status_code == 403
