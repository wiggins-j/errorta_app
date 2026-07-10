"""Tests for the F008b ComplianceGate."""
from __future__ import annotations

from errorta_briefs.compliance import (
    DEFAULT_LICENSE_ALLOWLIST,
    ComplianceGate,
    ComplianceRefusal,
)
from errorta_briefs.connector import SourceDoc


def _public_cc_by_doc(**overrides: object) -> SourceDoc:
    base = {
        "canonical_id": "ntrs:19990001234",
        "title": "An Aerodynamic Study",
        "source_url": "https://ntrs.nasa.gov/citations/19990001234",
        "publication_date": "1999-03-01",
        "sensitivity_class": "Public",
        "redistribution_allowed": True,
        "license": "CC-BY",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return SourceDoc(**base)  # type: ignore[arg-type]


def test_accepts_public_cc_by_doc_with_all_fields() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc())
    assert ok is True
    assert reason is None


def test_refuses_non_public_sensitivity() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc(sensitivity_class="Private"))
    assert ok is False
    assert reason is not None
    assert "sensitivity_class" in reason
    assert "Private" in reason


def test_refuses_when_redistribution_disallowed() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc(redistribution_allowed=False))
    assert ok is False
    assert reason is not None
    assert "redistribution_allowed" in reason


def test_refuses_missing_license() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc(license=None))
    assert ok is False
    assert reason is not None
    assert "license" in reason


def test_refuses_license_outside_allowlist() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc(license="Proprietary"))
    assert ok is False
    assert reason is not None
    assert "license" in reason
    assert "allowlist" in reason


def test_refuses_missing_source_url() -> None:
    gate = ComplianceGate()
    ok, reason = gate.accepts(_public_cc_by_doc(source_url=""))
    assert ok is False
    assert reason is not None
    assert "source_url" in reason


def test_refusal_reasons_are_distinct_across_each_failure_mode() -> None:
    gate = ComplianceGate()
    reasons: set[str] = set()
    for overrides in [
        {"sensitivity_class": "Private"},
        {"redistribution_allowed": False},
        {"license": None},
        {"license": "Proprietary"},
        {"source_url": ""},
    ]:
        ok, reason = gate.accepts(_public_cc_by_doc(**overrides))  # type: ignore[arg-type]
        assert ok is False
        assert reason is not None
        reasons.add(reason)
    assert len(reasons) == 5


def test_default_allowlist_includes_expected_licenses() -> None:
    assert "CC-BY" in DEFAULT_LICENSE_ALLOWLIST
    assert "CC-BY-SA" in DEFAULT_LICENSE_ALLOWLIST
    assert "CC0" in DEFAULT_LICENSE_ALLOWLIST
    assert "Public-Domain" in DEFAULT_LICENSE_ALLOWLIST
    assert "US-Gov-Work" in DEFAULT_LICENSE_ALLOWLIST


def test_custom_allowlist_overrides_default() -> None:
    gate = ComplianceGate(license_allowlist={"CC0"})
    ok, _ = gate.accepts(_public_cc_by_doc(license="CC0"))
    assert ok is True
    ok2, reason2 = gate.accepts(_public_cc_by_doc(license="CC-BY"))
    assert ok2 is False
    assert reason2 is not None
    assert "allowlist" in reason2


def test_refusal_log_record_has_canonical_id_reason_and_timestamp() -> None:
    gate = ComplianceGate()
    doc = _public_cc_by_doc(sensitivity_class="Private")
    ok, reason = gate.accepts(doc)
    assert ok is False
    assert reason is not None
    record = gate.refusal(doc, reason)
    assert isinstance(record, ComplianceRefusal)
    assert record.canonical_id == doc.canonical_id
    assert record.reason == reason
    # ISO-8601 UTC timestamp.
    assert "T" in record.occurred_at
    assert record.occurred_at.endswith("+00:00")
