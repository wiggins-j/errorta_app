"""F101-03 S3 — the T2 (consent-gated reduced isolation) gate on the run route.

A desktop app reaches T2 (runs without an OS sandbox) when its profile resolves
to no sandbox — either ``sandbox="none"`` or ``sandbox="auto"`` on a host with no
windowing sandbox. Run requires a SECOND explicit consent
(confirm_reduced_isolation) before it will execute — it is never the silent
default. The gate is keyed off the SAME backend the process manager resolves
(``resolve_sandbox_backend(profile.sandbox)``), not merely whether this host
*has* a sandbox, so the route can't disagree with the tier the run produces.
(The bwrap X11/Wayland carve-out itself landed in S2; it is Linux-only and not
runnable on macOS.)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from errorta_council.coding import runtime_process as rp
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runtime import RuntimeProfile, RuntimeProfileStore
from errorta_council.coding.workspace import CodingWorkspace

TAURI = {"x-errorta-origin": "tauri-ui"}


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(rp, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(rp, "_GRACE_SECONDS", 1.0)
    yield
    rp.teardown_all()


def _client() -> TestClient:
    from errorta_app.server import app
    return TestClient(app, headers=TAURI)


def _desktop_project(project_id: str, *, sandbox: str = "none") -> None:
    store = LedgerStore(project_id)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    ws = CodingWorkspace(project_id, store)
    ws.setup(target="new", repo_path=None)
    # A long-lived stand-in (a real GUI toolkit isn't available in this venv).
    (ws.root() / "game.py").write_text("import time\ntime.sleep(30)\n")
    RuntimeProfileStore.for_ledger(store).upsert_profile(RuntimeProfile(
        profile_id="default", project_id=project_id, kind="desktop",
        runtime_mode="managed_local", start=["python", "game.py"],
        sandbox=sandbox, health={"type": "none"}))


def _force_resolved_backend(monkeypatch, backend: str) -> None:
    """Pin ``resolve_sandbox_backend`` (the single source of truth the run route
    and the process manager both consult) so the consent gate is exercised
    independent of what this host actually has."""
    monkeypatch.setattr(
        "errorta_council.coding.runtime_process.resolve_sandbox_backend",
        lambda _requested: backend)


def test_preview_flags_t2_consent_for_reduced_isolation_profile(
        tmp_errorta_home: Path):
    # A ``sandbox="none"`` desktop profile reaches T2 and needs the second
    # consent — regression: this must hold even on a host that DOES have a
    # windowing sandbox (the gate keys off the resolved backend, not host
    # capability), so no monkeypatch here.
    _desktop_project("t2prev", sandbox="none")
    client = _client()

    r = client.post("/coding/projects/t2prev/runtime/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"] is True and body["runnable"] is True
    assert body["requires_reduced_isolation_consent"] is True
    assert body["session"] is None  # preview, no execution


def test_confirm_without_reduced_consent_is_refused(tmp_errorta_home: Path):
    _desktop_project("t2refuse", sandbox="none")
    client = _client()

    r = client.post("/coding/projects/t2refuse/runtime/run", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runnable"] is False
    assert body["reason"] == "reduced_isolation_consent_required"
    assert body["session"] is None
    # Refused: nothing executed.
    store = LedgerStore("t2refuse")
    assert RuntimeProfileStore.for_ledger(store).list_sessions() == []


def test_reduced_consent_allows_the_run(tmp_errorta_home: Path):
    _desktop_project("t2go", sandbox="none")
    client = _client()

    r = client.post("/coding/projects/t2go/runtime/run",
                    json={"confirm": True, "confirm_reduced_isolation": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runnable"] is True
    assert body["session"] is not None  # consent given -> it runs


def test_auto_profile_without_windowing_sandbox_needs_consent(
        tmp_errorta_home: Path, monkeypatch):
    # ``sandbox="auto"`` on a host with no seatbelt/bwrap resolves to ``none``
    # -> T2 -> consent required. Pin the resolution so the branch is exercised
    # regardless of what this host has.
    _desktop_project("t2auto", sandbox="auto")
    _force_resolved_backend(monkeypatch, "none")
    client = _client()

    r = client.post("/coding/projects/t2auto/runtime/run", json={})
    assert r.status_code == 200, r.text
    assert r.json()["requires_reduced_isolation_consent"] is True


def test_t1_profile_needs_no_reduced_consent(tmp_errorta_home: Path, monkeypatch):
    # A profile that resolves to a real OS sandbox (the macOS seatbelt case) runs
    # at T1 — no second consent. Pin the resolution so the assertion holds on any
    # host.
    _desktop_project("t1ok", sandbox="auto")
    _force_resolved_backend(monkeypatch, "seatbelt")
    client = _client()
    r = client.post("/coding/projects/t1ok/runtime/run", json={})
    assert r.status_code == 200, r.text
    assert r.json()["requires_reduced_isolation_consent"] is False
