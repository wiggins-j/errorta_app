"""Shared builders for the Council acceptance journeys (TS-03/TS-04).

A complete, schema-valid two-fake-member round-robin room. Fake members
(`fake.local.deterministic`) run offline and deterministically, so the journeys
are hermetic.
"""
from __future__ import annotations

from typing import Any


def _member(member_id: str, name: str, role: str) -> dict[str, Any]:
    return {
        "id": member_id, "name": name, "role": role, "enabled": True,
        "gateway_route_id": "fake.local.deterministic", "provider_kind": "local",
        "provider_display": "Fake", "model_display": "deterministic",
        "catalog_version": "2026-06-11",
        "context_access": "prompt_only", "transcript_access": "own_messages",
        "turn_limits": {"max_messages": 1, "max_input_tokens": 1024,
                        "max_output_tokens": 256, "max_context_tokens": 1024},
        "generation": {"temperature": 0.0, "top_p": None, "seed": None},
        "system_prompt": "", "metadata": {},
    }


def room_payload(room_id: str = "rm-acc", name: str = "Acceptance Room") -> dict[str, Any]:
    return {
        "format_version": 1, "id": room_id, "name": name, "description": "",
        "preset_id": None, "status_hint": "draft",
        "members": [
            _member("m-1", "M1", "answerer"),
            _member("m-2", "M2", "critic"),
        ],
        "topology": {"kind": "round_robin", "max_rounds": 1,
                     "max_total_turns": 2, "max_messages_per_member": 1,
                     "speaker_order": ["m-1", "m-2"],
                     "allow_user_interjection": False, "stop_when": {}},
        "context_policy": {"default_context_access": "prompt_only",
                           "default_transcript_access": "own_messages",
                           "allow_full_context": False,
                           "require_confirmation_for_remote_context": True,
                           "require_confirmation_for_full_context": True,
                           "member_overrides": {},
                           "redaction_profile_id": None, "summary_profile_id": None},
        "budget_policy": {"max_rounds": 1, "max_messages_per_member": 1,
                          "max_total_model_calls": 2, "max_remote_calls_per_run": 0,
                          "max_remote_calls_per_day": None,
                          "max_input_tokens_per_turn": 1024,
                          "max_output_tokens_per_turn": 256,
                          "max_context_tokens_per_member": 1024,
                          "max_estimated_usd_per_run": 0.0,
                          "max_estimated_usd_per_month": None,
                          "warn_at_fraction": [], "on_budget_exhausted": "stop",
                          "require_confirmation_before_first_remote_call": True,
                          "require_confirmation_above_estimated_usd": None},
        "finalization_policy": {"mode": "transcript_only",
                                "finalizer_member_id": None,
                                "judge_member_ids": [],
                                "require_judge_verdict": False,
                                "allow_minority_report": True,
                                "allow_grounding_write": False,
                                "grounding_requires_user_accept": True},
        "ui": {}, "created_at": "2026-06-11T00:00:00Z",
        "updated_at": "2026-06-11T00:00:00Z",
        "last_validated_at": None, "revision": 1,
    }
