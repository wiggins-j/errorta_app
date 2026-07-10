"""F145 anti-drift canary for the PM operator reference.

The prose remains reviewable documentation; the embedded JSON is the compact,
machine-readable claim set that must stay synchronized with executable schemas.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from errorta_app.routes.coding import _RunSetupConfirmBody, router as coding_router
from errorta_app.routes.council import router as council_router
from errorta_council.coding.autonomy import (
    CADENCE_EVERY_N,
    CADENCE_OFF,
    CADENCE_ON_MERGE_READY,
    CADENCE_PER_MILESTONE,
    CodingAutonomyPolicy,
    policy_to_dict,
)
from errorta_council.coding.governance import GovernanceMode, HumanCodeApproval
from errorta_council.coding.runtime import (
    PROFILE_KINDS,
    RUNTIME_MODES,
    SANDBOX_CHOICES,
)
from errorta_council.coding.runtime_launchers import registered_modalities
from errorta_council.coding.topology import DEV, PM, REVIEWER, TESTER
from errorta_council.validation import MODEL_MODES, PM_MODEL_MODES
from errorta_model_gateway.policy import VALID_PROVIDERS
from errorta_project_grounding.corpus_binding import VALID_BINDING_MODES

_START = "<!-- PM_REFERENCE_CONTRACT_START -->"
_END = "<!-- PM_REFERENCE_CONTRACT_END -->"
_REFERENCE = Path(__file__).parents[3] / "docs" / "coding" / "PM_REFERENCE.md"


def _contract() -> dict[str, object]:
    text = _REFERENCE.read_text(encoding="utf-8")
    payload = text.split(_START, 1)[1].split(_END, 1)[0].strip()
    assert payload.startswith("```json\n") and payload.endswith("```")
    return json.loads(payload.removeprefix("```json\n").removesuffix("```").strip())


def _sorted(values: object) -> list[str]:
    return sorted(str(value) for value in values)  # type: ignore[union-attr]


def test_reference_contract_matches_executable_schemas() -> None:
    contract = _contract()

    assert contract["schema_version"] == 1
    assert contract["provider_classes"] == _sorted(VALID_PROVIDERS - {"off"})
    assert contract["coding_roles"] == sorted([PM, DEV, REVIEWER, TESTER])
    assert contract["model_modes"] == _sorted(MODEL_MODES)
    assert contract["pm_model_modes"] == _sorted(PM_MODEL_MODES)
    assert contract["run_setup_fields"] == sorted(_RunSetupConfirmBody.model_fields)
    assert contract["autonomy_defaults"] == policy_to_dict(CodingAutonomyPolicy())
    assert contract["checkpoint_cadences"] == sorted([
        CADENCE_OFF,
        CADENCE_EVERY_N,
        CADENCE_PER_MILESTONE,
        CADENCE_ON_MERGE_READY,
    ])
    assert contract["governance_modes"] == _sorted(get_args(GovernanceMode))
    assert contract["human_code_approval"] == _sorted(get_args(HumanCodeApproval))
    assert contract["runtime_profile_kinds"] == _sorted(PROFILE_KINDS)
    assert contract["runtime_modes"] == _sorted(RUNTIME_MODES)
    assert contract["sandbox_choices"] == _sorted(SANDBOX_CHOICES)
    assert contract["grounding_modes"] == _sorted(VALID_BINDING_MODES)

    declared_unimplemented = {"emulation", "mobile"}
    modalities = set(registered_modalities())
    assert contract["declared_unimplemented_modalities"] == sorted(
        declared_unimplemented
    )
    assert contract["implemented_modalities"] == sorted(
        modalities - declared_unimplemented
    )


def test_reference_control_routes_exist() -> None:
    contract = _contract()
    actual = {
        (method, route.path)
        for router in (coding_router, council_router)
        for route in router.routes
        for method in (route.methods or set())
    }
    documented = {
        (str(item["method"]), str(item["path"]))
        for item in contract["control_routes"]  # type: ignore[union-attr]
    }

    assert documented <= actual
    assert ("POST", "/coding/projects/{project_id}/run/pause") not in documented


def test_run_setup_rejects_non_contract_approval_values() -> None:
    for stale_value in ("per_stage", "autonomous"):
        with pytest.raises(ValidationError):
            _RunSetupConfirmBody(human_code_approval=stale_value)
