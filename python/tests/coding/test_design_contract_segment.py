"""Slice 1 §4/§9 — the design_contract prompt segment.

With an approved design_spec the DEV and REVIEWER prompts carry the contract
(tokens, direction, relevant screen intent). Without one they are byte-identical to
today — that absence contract is what the golden prompt-segment test locks.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.governance import GovernanceStore
from errorta_council.coding.ledger import LedgerStore
from errorta_council.coding.runner import (
    _design_contract_text,
    _dev_prompt,
    _review_pr_prompt,
)


def _store(pid: str) -> LedgerStore:
    store = LedgerStore(pid)
    store.create_project(north_star="n", definition_of_done="d",
                         target="new", repo_path=None)
    return store


def _approve_design(store: LedgerStore) -> None:
    GovernanceStore.for_ledger(store).append_artifact(
        kind="design_spec", title="Design contract",
        body_markdown="Warm editorial; do favor whitespace; don't use pure black.",
        body_json={
            "direction_matrix": {"typography": "humanist sans", "color": "warm",
                                 "density": "comfortable", "shape": "rounded",
                                 "motion": "subtle", "era_mood": "modern"},
            "tokens": {"palette": {"bg": "#faf7f2"}, "spacing": {"md": "16px"}},
            "assets": {"font_family_ids": ["inter"], "icon_set_id": "lucide"},
            "screens": [{"screen": "home", "purpose": "land", "layout": "hero",
                         "hierarchy": "title>cta", "key_states": ["default"]}],
            "components": [{"name": "button", "usage": "primary"}],
        },
        state="approved")


def test_no_design_spec_renders_empty(tmp_errorta_home: Path) -> None:
    store = _store("dc-empty")
    assert _design_contract_text(store) == ""


def test_dev_prompt_gains_contract_when_approved(tmp_errorta_home: Path) -> None:
    store = _store("dc-dev")
    _approve_design(store)
    task = store.add_task(title="Build the home screen", role="dev",
                          detail="lay out the hero")
    prompt = _dev_prompt(task, store)
    assert "DESIGN CONTRACT" in prompt
    assert "Tokens:" in prompt
    # task-relevant screen intent is matched by name ("home").
    assert "home" in prompt and "hero" in prompt


def test_reviewer_prompt_gains_contract_when_approved(tmp_errorta_home: Path) -> None:
    store = _store("dc-rev")
    _approve_design(store)
    task = store.add_task(title="review PR: home screen", role="reviewer")
    pr = {"branch": "feat/home", "head": "abc123", "task_id": task.task_id}
    contract = _design_contract_text(store, task)
    prompt = _review_pr_prompt(task, pr, "diff --git a/x b/x\n+1\n", "ctx\n",
                               design_contract=contract)
    assert "DESIGN CONTRACT" in prompt
