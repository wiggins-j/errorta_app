from __future__ import annotations

import json
from pathlib import Path

from errorta_model_gateway.policy import GatewayPolicy
from errorta_model_gateway.settings import load_policy, save_policy


def test_load_missing_policy_defaults_local_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))

    policy = load_policy()

    assert policy.global_mode == "local_only"
    assert policy.route_for("answerer").provider == "local"
    assert policy.egress_policy_for("anything") == "local_only"


def test_policy_round_trip_filters_unknown_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    path = tmp_path / "model-gateway" / "policy.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "global_mode": "user_selected",
                "role_routes": {
                    "judge": {"provider": "anthropic", "model": "claude"},
                    "bogus": {"provider": "anthropic"},
                    "answerer": {"provider": "bad-provider"},
                },
                "corpus_policies": {
                    "welcome": "redacted_support",
                    "bad": "full_remote_answering",
                },
                "budget": {
                    "max_tokens_per_call": 400,
                    "max_remote_calls_per_session": 3,
                    "max_usd_per_month": 9.5,
                },
            }
        ),
        encoding="utf-8",
    )

    policy = load_policy()

    assert policy.global_mode == "you_pick"
    assert policy.route_for("judge").provider == "anthropic"
    assert policy.route_for("judge").model == "claude"
    assert "bogus" not in policy.role_routes
    assert policy.route_for("answerer").provider == "local"
    assert policy.corpus_policies == {"welcome": "redacted_support"}
    assert policy.budget.max_tokens_per_call == 400
    assert policy.budget.max_remote_calls_per_session == 3
    assert policy.budget.max_usd_per_month == 9.5


def test_save_policy_writes_canonical_atomic_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    policy = GatewayPolicy.from_dict(
        {
            "global_mode": "you_pick",
            "corpus_policies": {"welcome": "answer_context"},
            "budget": {"max_tokens_per_call": 800},
        }
    )

    saved = save_policy(policy)
    payload = json.loads(
        (tmp_path / "model-gateway" / "policy.json").read_text(encoding="utf-8")
    )

    assert saved.updated_at
    assert payload["global_mode"] == "you_pick"
    assert payload["corpus_policies"] == {"welcome": "answer_context"}
    assert payload["budget"]["max_tokens_per_call"] == 800
    assert payload["updated_at"] == saved.updated_at
