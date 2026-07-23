"""Spec 01 — run caps are observable.

Two surfaces, one guarantee: a cap read back from ``autonomy.json`` reports its
persisted value AND whether it was explicitly set (vs served from the built-in
default). ``policy_with_provenance`` is the read side; ``render_status`` is the
CLI display side (the ``caps:`` line + a ``(default)`` marker).
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_cli.render.status import render_status
from errorta_cli.verbosity import Verbosity
from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    policy_from_dict,
    policy_with_provenance,
    save_policy,
)
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path) -> LedgerStore:
    s = LedgerStore("caps-proj", root=tmp_path)
    s.create_project(
        north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def test_persisted_cap_is_reported_and_not_defaulted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # save_policy serializes the FULL policy, so every cap key lands on disk.
    save_policy(store, policy_from_dict({"max_iterations": 40}))

    policy, defaulted = policy_with_provenance(store)

    assert policy["max_iterations"] == 40
    assert defaulted == []  # a full save leaves nothing defaulted


def test_partial_autonomy_json_marks_absent_caps_defaulted(tmp_path: Path) -> None:
    # A file that only carries `max_iterations` (older writer / hand edit) — the
    # other three caps are served from the default and must be flagged.
    store = _store(tmp_path)
    (store.dir / "autonomy.json").write_text(
        json.dumps({"max_iterations": 40}), encoding="utf-8")

    policy, defaulted = policy_with_provenance(store)

    assert policy["max_iterations"] == 40
    assert "max_iterations" not in defaulted
    assert set(defaulted) == {
        "max_model_calls",
        "max_parallel_workers",
        "delivery_review_round_limit",
    }


def test_missing_autonomy_json_defaults_every_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert not (store.dir / "autonomy.json").exists()

    policy, defaulted = policy_with_provenance(store)

    assert policy["max_iterations"] == CodingAutonomyPolicy().max_iterations == 200
    assert set(defaulted) == {
        "max_iterations",
        "max_model_calls",
        "max_parallel_workers",
        "delivery_review_round_limit",
    }


def test_render_status_shows_caps_line_and_marks_defaults() -> None:
    payload = {
        "project_id": "caps-proj",
        "health": {"service": "errorta", "version": "1", "python": "3.14"},
        "run": {
            "running": False,
            "state": {"status": "idle"},
            "caps": {
                "max_iterations": 40,
                "max_model_calls": None,
                "max_parallel_workers": 3,
                "delivery_review_round_limit": 3,
                "defaulted": ["max_parallel_workers", "delivery_review_round_limit"],
            },
        },
    }

    # Collapse rich's line-wrapping (console width) so the assertions are about
    # content, not wrap points.
    out = " ".join(render_status(payload, Verbosity()).split())

    assert "caps:" in out
    assert "iterations 40" in out
    # max_model_calls == None renders as the infinity glyph.
    assert "model_calls ∞" in out
    # Only the defaulted caps carry the marker.
    assert "parallel 3 (default)" in out
    assert "delivery_rounds 3 (default)" in out
    assert "iterations 40 (default)" not in out


def test_render_status_omits_caps_line_for_older_server() -> None:
    payload = {
        "project_id": "caps-proj",
        "health": {"service": "errorta", "version": "1", "python": "3.14"},
        "run": {"running": False, "state": {"status": "idle"}},
    }

    out = render_status(payload, Verbosity())

    assert "caps:" not in out
