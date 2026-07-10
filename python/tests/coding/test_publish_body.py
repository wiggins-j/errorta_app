"""F102 Slice E — publish body builder (pure, redacted)."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_council.coding.publish_body import build_publish_body


def _project(project_id: str, *, north_star: str):
    from errorta_council.coding.ledger import LedgerStore
    store = LedgerStore(project_id)
    store.create_project(
        north_star=north_star, definition_of_done="d", target="new",
        repo_path=None)
    return store


def test_body_includes_north_star_and_built_titles(tmp_errorta_home: Path) -> None:
    store = _project("body-1", north_star="Build a CLI calculator")
    t1 = store.add_task(title="implement add", role="dev")
    t2 = store.add_task(title="implement subtract", role="dev")
    store.update_task(t1.task_id, state="done")
    store.update_task(t2.task_id, state="done")

    body = build_publish_body(store)
    assert "North Star" in body
    assert "Build a CLI calculator" in body
    assert "What was built" in body
    assert "implement add" in body
    assert "implement subtract" in body
    assert "2/2 planned tasks complete" in body


def test_body_redacts_tokens_and_home_path(tmp_errorta_home: Path) -> None:
    home = str(Path.home())
    store = _project(
        "body-redact",
        north_star=f"deploy with ghp_0123456789abcdefghij0123456789abcd from {home}/secrets")
    body = build_publish_body(store)
    assert "ghp_0123456789" not in body
    assert home not in body


def test_body_omits_runtime_line_when_no_evidence(tmp_errorta_home: Path) -> None:
    store = _project("body-noruntime", north_star="ns")
    body = build_publish_body(store)
    assert "Runtime checks (F101)" not in body
    assert "Published by Errorta Coding Team." in body


def test_body_includes_runtime_line_when_evidence_present(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _project("body-runtime", north_star="ns")
    from errorta_council.coding import publish_body

    monkeypatch.setattr(publish_body, "store_head", lambda s: "headsha")

    class _FakeRStore:
        @classmethod
        def for_ledger(cls, s):  # noqa: ANN001
            return cls()

    monkeypatch.setattr(
        "errorta_council.coding.runtime.RuntimeProfileStore", _FakeRStore)
    monkeypatch.setattr(
        "errorta_council.coding.runtime.latest_runtime_evidence",
        lambda rstore, current_head: {
            "results": [
                {"passed": True, "fresh": True, "head": "headsha"},
                {"passed": False, "fresh": False, "head": "old"},
            ],
            "any_fresh_pass": True, "current_head": current_head})

    body = build_publish_body(store)
    assert "Runtime checks (F101): 1/2 passed" in body
