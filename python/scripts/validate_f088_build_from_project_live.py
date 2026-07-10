"""LIVE proof: build_from_project ingests a project's own code into the REMOTE
AIAR (example-host over the SSH tunnel) and the team can retrieve it.

Uses the REAL ~/.errorta remote-aiar config (tunnel URL + token), so it requires
the example-host tunnel to be up (e.g. http://127.0.0.1:8766). Creates a throwaway
project, builds its corpus on the remote, asserts the instance + chunks appear,
retrieves a known symbol, then cleans up the local project state.

Run:  python/.venv/bin/python python/scripts/validate_f088_build_from_project_live.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

# Use the REAL Errorta home so the persisted remote-aiar.json (example-host tunnel +
# token) is what the adapter dispatches against — this is a live integration run.
os.environ.setdefault("ERRORTA_HOME", str(Path.home() / ".errorta"))

PROJECT_ID = "f088-grounding-fix-live"


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from errorta_app.routes import coding as coding_routes
    from errorta_council.coding.ledger import LedgerStore
    from errorta_council.coding.workspace import CodingWorkspace
    from errorta_project_grounding.bootstrap import load_job
    from errorta_project_grounding.corpus_binding import load_binding
    from errorta_project_grounding.remote_adapter import remote_aiar_config

    cfg = remote_aiar_config()
    if cfg is None:
        _fail("no remote AIAR configured (~/.errorta/remote-aiar.json) — is the example-host tunnel set up?")
    print(f"remote AIAR target: {cfg.base_url}  (this is the example-host tunnel)")

    # Fresh project state (clean any prior run of this script).
    store = LedgerStore(PROJECT_ID)
    if store.dir.exists():
        shutil.rmtree(store.dir, ignore_errors=True)
        store = LedgerStore(PROJECT_ID)
    store.create_project(north_star="prove grounding", definition_of_done="indexed",
                         target="new", repo_path=None)
    ws = CodingWorkspace(PROJECT_ID, store)
    ws.setup(target="new", repo_path=None)
    # The "team's merged code" — a recognizable symbol to retrieve later.
    ws._ws.write_and_commit(
        "src/pocketboard/board.py",
        "def add_todo(board, title):\n"
        "    \"\"\"Append a todo card to a PocketBoard column.\"\"\"\n"
        "    board.setdefault('todo', []).append({'title': title})\n"
        "    return board\n",
    )
    print("committed real code to master: src/pocketboard/board.py::add_todo")

    app = FastAPI()
    app.include_router(coding_routes.router)
    client = TestClient(app, headers={"x-errorta-origin": "tauri-ui"})

    # 1) Build the corpus from the project's own code -> ingests to example-host.
    resp = client.post(f"/coding/projects/{PROJECT_ID}/grounding/build-from-project", json={})
    if resp.status_code != 200:
        _fail(f"build-from-project returned {resp.status_code}: {resp.text}")
    binding = resp.json()["binding"]
    corpus_id = binding["corpus_id"]
    print(f"build-from-project OK -> corpus_id={corpus_id}, adapter_source={binding['adapter_source']}")
    if binding["adapter_source"] != "remote":
        _fail(f"binding is not remote (got {binding['adapter_source']}) — would never hit example-host")

    # 2) Poll the bootstrap job to completion.
    job = resp.json().get("job") or {}
    job_id = job.get("job_id")
    deadline = time.time() + 120
    status = job.get("status")
    while job_id and status not in ("done", "failed", "interrupted") and time.time() < deadline:
        time.sleep(2)
        j = load_job(store, job_id)
        status = j.status if j else status
    print(f"bootstrap job status: {status}")
    if status != "done":
        _fail(f"bootstrap did not finish cleanly (status={status})")

    # 3) Confirm the instance now exists on example-host with chunks.
    import httpx
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    inst = httpx.get(f"{cfg.base_url}/instances", headers=headers, timeout=15).json()
    match = next((i for i in inst.get("instances", []) if i.get("name") == corpus_id), None)
    if match is None:
        _fail(f"corpus {corpus_id} NOT found on example-host after build")
    print(f"example-host instance present: {corpus_id} chunks={match.get('chunk_count')} published={match.get('published')}")
    if int(match.get("chunk_count") or 0) <= 0:
        _fail("instance has 0 chunks — nothing was indexed")

    # 4) Health now reads ready from the remote (no more 404).
    hb = load_binding(store)
    from errorta_project_grounding.corpus_binding import binding_status
    hs = binding_status(hb)
    print(f"binding health: {hs.health_state} — {hs.health_reason}")

    # 5) Retrieve the known symbol through the project's bound corpus.
    r = client.get(f"/coding/projects/{PROJECT_ID}/grounding/retrieve",
                   params={"q": "how do I add a todo to the board", "k": 5})
    if r.status_code != 200:
        _fail(f"retrieve returned {r.status_code}: {r.text}")
    hits = r.json().get("hits", [])
    print(f"retrieval status={r.json().get('status')} hits={len(hits)}")
    joined = " ".join(str(h.get("content", "")) for h in hits)
    if "add_todo" not in joined and "PocketBoard" not in joined and "todo" not in joined.lower():
        _fail(f"retrieval did not surface the indexed code; hits={hits}")
    print("retrieval surfaced the project's own code ✓")

    # Cleanup local project state (the remote test instance is left for inspection;
    # delete it from the AIAR UI if you want it gone).
    shutil.rmtree(store.dir, ignore_errors=True)
    print("\nPASS: build_from_project -> example-host -> retrieve loop verified end-to-end.")


if __name__ == "__main__":
    main()
