"""F047 — declarative Council profiles: export/import round-trip + validation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from errorta_council.profiles import (
    export_room_to_profile,
    import_profile_to_room_draft,
    profile_to_yaml,
)
from errorta_council.profiles.examples import all_examples
from errorta_council.profiles.schema import (
    ProfileError,
    parse_profile_yaml,
    validate_profile_shape,
)

NOW = "2026-06-14T00:00:00Z"


def _room_dict():
    return {
        "format_version": 1,
        "id": "room-xyz",
        "name": "My Council",
        "description": "desc",
        "revision": 7,
        "created_at": NOW,
        "updated_at": NOW,
        "last_validated_at": NOW,
        "status_hint": "ready",
        "ui": {"scroll": 42},
        "members": [
            {
                "id": "m-1", "name": "Gem", "role": "member", "enabled": True,
                "provider_kind": "local", "gateway_route_id": "local.ollama.gemma3:27b",
                "model": "gemma3:27b", "provider_display": "Local",
                "model_display": "gemma3:27b", "catalog_version": "2026-06-12",
                "context_access": "prompt_only", "transcript_access": "all_messages",
                "turn_limits": {"max_messages": 1}, "generation": {"temperature": 0.3},
                "system_prompt": "Be terse.", "metadata": {},
            },
        ],
        "topology": {"kind": "consensus_deliberation", "max_rounds": 3},
        "finalization_policy": {"mode": "consensus_report"},
    }


def test_export_strips_runtime_and_identity_fields():
    profile = export_room_to_profile(_room_dict())
    for runtime in ("id", "revision", "created_at", "updated_at",
                    "last_validated_at", "status_hint", "ui"):
        assert runtime not in profile, runtime
    # Per-member runtime/derived bindings stripped.
    m = profile["members"][0]
    for runtime in ("catalog_version", "provider_display", "model_display"):
        assert runtime not in m, runtime
    # Intent preserved.
    assert m["system_prompt"] == "Be terse."
    assert profile["topology"]["kind"] == "consensus_deliberation"
    assert profile["finalization_policy"]["mode"] == "consensus_report"


def test_export_refuses_secret_keys():
    room = _room_dict()
    room["members"][0]["metadata"] = {"api_key": "sk-LEAK"}
    with pytest.raises(ProfileError):
        export_room_to_profile(room)


def test_budget_token_caps_are_not_treated_as_secrets():
    # Regression: 'token' substring must not flag max_*_tokens_* budget caps.
    room = _room_dict()
    room["budget_policy"] = {
        "max_input_tokens_per_turn": 4096,
        "max_output_tokens_per_turn": 2048,
        "max_context_tokens_per_member": 4096,
    }
    profile = export_room_to_profile(room)  # must NOT raise
    assert profile["budget_policy"]["max_input_tokens_per_turn"] == 4096
    # And it round-trips back through import (which also runs the secret guard).
    result = import_profile_to_room_draft(
        profile, available_provider_classes=set(), available_tool_ids=set(), now=NOW
    )
    assert result["room"]["budget_policy"]["max_output_tokens_per_turn"] == 2048
    # A real token secret IS still caught.
    room["budget_policy"]["access_token"] = "sk-LEAK"
    with pytest.raises(ProfileError):
        export_room_to_profile(room)


def test_round_trip_room_to_profile_to_draft_preserves_policy():
    profile = export_room_to_profile(_room_dict())
    result = import_profile_to_room_draft(
        profile,
        available_provider_classes=set(),  # local is always available
        available_tool_ids=set(),
        now=NOW,
    )
    draft = result["room"]
    assert draft["status_hint"] == "draft"
    assert draft["revision"] == 0
    assert draft["id"] != "room-xyz"  # fresh id, not the original
    assert draft["topology"]["kind"] == "consensus_deliberation"
    assert draft["finalization_policy"]["mode"] == "consensus_report"
    # Draft is a valid CouncilRoom (member required fields backfilled).
    from errorta_council.schema import CouncilRoom

    room = CouncilRoom.from_dict(draft)
    assert room.members[0].system_prompt == "Be terse."
    assert room.members[0].id == "m-1"


def test_import_reports_missing_provider_without_remapping():
    profile = {
        "format_version": 1, "name": "Remote", "members": [
            {"id": "claude", "gateway_route_id": "anthropic.claude-opus-4-8",
             "model": "opus"},
        ],
    }
    result = import_profile_to_room_draft(
        profile, available_provider_classes=set(), available_tool_ids=set(), now=NOW
    )
    mp = result["validation"]["missing_providers"]
    assert len(mp) == 1 and mp[0]["provider_class"] == "anthropic"
    # NOT remapped to local/fake — the intended route is preserved.
    assert result["room"]["members"][0]["gateway_route_id"] == "anthropic.claude-opus-4-8"


def test_import_reports_requested_and_missing_tools():
    profile = {
        "format_version": 1, "name": "Coder",
        "members": [{"id": "p", "gateway_route_id": "local.ollama.x"}],
        "tool_policy": {"code_read": {"enabled": True}, "code_exec": {"enabled": True}},
    }
    # code_read configured, code_exec not.
    result = import_profile_to_room_draft(
        profile, available_provider_classes=set(),
        available_tool_ids={"code_read"}, now=NOW,
    )
    v = result["validation"]
    assert set(v["requested_tools"]) == {"code_read", "code_exec"}
    assert v["missing_tools"] == ["code_exec"]


def test_yaml_round_trip_and_safe_parse():
    profile = all_examples()["brainstorm-council"]
    text = profile_to_yaml(profile)
    parsed = parse_profile_yaml(text)
    assert parsed["name"] == "Brainstorm Council"
    assert validate_profile_shape(parsed) == []
    with pytest.raises(ProfileError):
        parse_profile_yaml("- just\n- a\n- list")


def test_example_profiles_are_valid_and_coding_has_no_exec():
    examples = all_examples()
    assert set(examples) == {"brainstorm-council", "coding-council", "credibility-council"}
    for profile in examples.values():
        assert validate_profile_shape(profile) == []
    coding = examples["coding-council"]
    # Coding council is propose-only with NO exec until granted.
    assert coding["tool_policy"]["code_exec"]["enabled"] is False
    assert coding["tool_policy"]["code_write"]["mode"] == "propose_only"


def test_profile_routes_export_validate_examples(tmp_path, monkeypatch):
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    from errorta_app.server import app

    client = TestClient(app)

    # Examples.
    ex = client.get("/council/profiles/examples")
    assert ex.status_code == 200
    slugs = {e["slug"] for e in ex.json()["examples"]}
    assert slugs == {"brainstorm-council", "coding-council", "credibility-council"}

    # Validate (no save) from a profile dict.
    profile = all_examples()["brainstorm-council"]
    resp = client.post("/council/profiles/validate", json={"profile": profile})
    assert resp.status_code == 200
    body = resp.json()
    assert body["room"]["status_hint"] == "draft"
    assert body["validation"]["ok"] is True
    # No room was persisted by validate.
    assert client.get("/council/rooms").json()["rooms"] == []

    # Validate from YAML text.
    resp2 = client.post("/council/profiles/validate", json={"yaml": profile_to_yaml(profile)})
    assert resp2.status_code == 200

    # Bad YAML -> 422.
    bad = client.post("/council/profiles/validate", json={"yaml": "{not: valid: yaml:"})
    assert bad.status_code == 422
