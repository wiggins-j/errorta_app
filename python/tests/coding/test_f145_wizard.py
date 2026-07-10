"""F145 Slice 2 — the AI Wizard conversation + runnable-by-construction gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from errorta_council.coding import wizard as W


def _caller(reply_obj):
    """A fake model that returns a fixed JSON turn (or raw text)."""
    def call(member, prompt):
        return reply_obj if isinstance(reply_obj, str) else json.dumps(reply_obj)
    return call


_FULL = {
    "reply": "Great — I have what I need.",
    "charter": {
        "north_star": "A tip-split calculator",
        "audience": "friends splitting a bill",
        "modality": "static",
        "definition_of_done": "opens in a browser and updates live",
        "entrypoint": "index.html",
        "team_recipe": "fast_cheap",
        "autonomous": True,
    },
    "ready": True,
    "missing": [],
}


def test_session_roundtrip(tmp_errorta_home: Path):
    s = W.new_session("local.q")
    assert W.get_session(s.session_id) is not None
    W.discard_session(s.session_id)
    assert W.get_session(s.session_id) is None


def test_turn_captures_charter_and_readiness(tmp_errorta_home: Path):
    s = W.new_session("local.q")
    s = W.run_turn(s, "build me a tip splitter", context="CTX",
                   caller=_caller(_FULL))
    assert s.ready is True
    assert s.missing == []
    assert s.charter["modality"] == "static"
    assert s.messages[-1]["role"] == "pm"


def test_completion_contract_not_ready_when_fields_missing(tmp_errorta_home: Path):
    partial = {
        "reply": "what kind of app?",
        "charter": {"north_star": "something"},
        "ready": True,  # model claims ready — must be OVERRIDDEN
        "missing": [],
    }
    s = W.new_session("local.q")
    s = W.run_turn(s, "hi", context="CTX", caller=_caller(partial))
    # readiness is enforced by the required-field check, not trusted to the model
    assert s.ready is False
    assert set(W.REQUIRED_CHARTER_FIELDS) - {"north_star"} <= set(s.missing)
    with pytest.raises(W.WizardError):
        W.finalize(s)


def test_charter_values_accumulate_across_turns(tmp_errorta_home: Path):
    s = W.new_session("local.q")
    s = W.run_turn(s, "a cli tool", context="CTX", caller=_caller({
        "reply": "ok", "charter": {"north_star": "a todo cli", "modality": "cli"},
        "ready": False, "missing": []}))
    s = W.run_turn(s, "for me", context="CTX", caller=_caller({
        "reply": "ok", "charter": {
            "audience": "me", "definition_of_done": "prints tasks, exits 0",
            "entrypoint": "main.py", "team_recipe": "balanced",
            "autonomous": False}, "ready": True, "missing": []}))
    # earlier north_star/modality survive the later turn
    assert s.charter["north_star"] == "a todo cli"
    assert s.charter["modality"] == "cli"
    assert s.ready is True
    charter = W.finalize(s)
    assert charter["modality"] == "cli" and charter["entrypoint"] == "main.py"


def test_not_ready_until_team_and_autonomy_are_asked(tmp_errorta_home: Path):
    # All FIVE charter strings are filled, but the team was never discussed —
    # the Wizard must still refuse to finish until team_recipe + autonomous are set.
    no_team = {
        "reply": "looks good?",
        "charter": {
            "north_star": "a todo cli", "audience": "me", "modality": "cli",
            "definition_of_done": "prints tasks, exits 0", "entrypoint": "main.py",
        },
        "ready": True,  # model claims ready — must be OVERRIDDEN
        "missing": [],
    }
    s = W.new_session("local.q")
    s = W.run_turn(s, "go", context="CTX", caller=_caller(no_team))
    assert s.ready is False
    assert "team_recipe" in s.missing and "autonomous" in s.missing
    with pytest.raises(W.WizardError):
        W.finalize(s)
    # answering both (autonomous=False is a VALID answer — presence, not truthiness)
    # clears the gate.
    s = W.run_turn(s, "balanced, and check in with me", context="CTX", caller=_caller({
        "reply": "great", "charter": {"team_recipe": "balanced", "autonomous": False},
        "ready": True, "missing": []}))
    assert s.ready is True and s.missing == []
    assert W.finalize(s)["autonomous"] is False


def test_lenient_parse_of_fenced_json(tmp_errorta_home: Path):
    wrapped = "Sure!\n```json\n" + json.dumps(_FULL) + "\n```\nlet me know"
    s = W.new_session("local.q")
    s = W.run_turn(s, "go", context="CTX", caller=_caller(wrapped))
    assert s.ready is True and s.charter["entrypoint"] == "index.html"


def test_non_json_reply_degrades_gracefully(tmp_errorta_home: Path):
    s = W.new_session("local.q")
    s = W.run_turn(s, "go", context="CTX", caller=_caller("just some prose, no json"))
    assert s.ready is False
    assert s.messages[-1]["text"] == "just some prose, no json"


def test_model_unreachable_raises_wizard_error(tmp_errorta_home: Path):
    def boom(member, prompt):
        raise RuntimeError("egress down")
    s = W.new_session("local.q")
    with pytest.raises(W.WizardError):
        W.run_turn(s, "go", context="CTX", caller=boom)


def test_autonomous_string_false_is_not_truthy(tmp_errorta_home: Path):
    # a model emitting the string "false" must NOT yield an autonomous project
    obj = dict(_FULL)
    obj["charter"] = dict(_FULL["charter"], autonomous="false")
    s = W.new_session("local.q")
    s = W.run_turn(s, "go", context="CTX", caller=_caller(obj))
    assert W.finalize(s)["autonomous"] is False


def test_finalize_rejects_invalid_modality(tmp_errorta_home: Path):
    s = W.new_session("local.q")
    bad = dict(_FULL)
    bad["charter"] = dict(_FULL["charter"], modality="hologram")
    s = W.run_turn(s, "go", context="CTX", caller=_caller(bad))
    # required fields present so ready, but modality invalid -> finalize refuses
    with pytest.raises(W.WizardError):
        W.finalize(s)
