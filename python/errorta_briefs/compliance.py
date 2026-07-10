"""Compliance gate for brief-driven collection (F008).

Every ``SourceDoc`` returned by a ``SourceConnector`` must clear the
``ComplianceGate`` before it is fetched, persisted, or surfaced to the user.
The gate enforces four rules in a fixed order so refusal reasons are
deterministic and easy to log:

1. Required fields populated (canonical_id, title, source_url,
   sensitivity_class, redistribution_allowed, license).
2. ``sensitivity_class == "Public"``.
3. ``redistribution_allowed is True``.
4. ``license`` is in the allowlist.

The default allowlist matches the aerospace-corpus precedent: CC-BY, CC-BY-SA,
CC0, Public-Domain, US-Gov-Work, and PSF-2.0 (the Python Software Foundation
license, used by the official Python documentation at docs.python.org).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .connector import SourceDoc


DEFAULT_LICENSE_ALLOWLIST: frozenset[str] = frozenset(
    {"CC-BY", "CC-BY-SA", "CC0", "Public-Domain", "US-Gov-Work", "PSF-2.0"}
)

# Fields that must be non-null / non-empty for a SourceDoc to be acceptable.
# ``redistribution_allowed`` is a bool, so "populated" means "not None".
_REQUIRED_FIELDS: tuple[str, ...] = (
    "canonical_id",
    "title",
    "source_url",
    "sensitivity_class",
    "redistribution_allowed",
    "license",
)


@dataclass
class ComplianceRefusal:
    """A structured log record of one refused document."""

    canonical_id: str
    reason: str
    occurred_at: str


class ComplianceGate:
    """Decide whether a ``SourceDoc`` may be ingested.

    The gate is intentionally simple and stateless. Callers build one per run
    and reuse it across every connector. Downstream code logs refusals via
    ``ComplianceRefusal`` for after-the-fact auditing.
    """

    def __init__(self, license_allowlist: set[str] | frozenset[str] | None = None) -> None:
        if license_allowlist is None:
            self.license_allowlist: frozenset[str] = DEFAULT_LICENSE_ALLOWLIST
        else:
            self.license_allowlist = frozenset(license_allowlist)

    def accepts(self, doc: SourceDoc) -> tuple[bool, str | None]:
        """Return ``(True, None)`` if ``doc`` clears every rule, else
        ``(False, reason)`` for the first failing rule.
        """
        # Rule 1: required fields populated.
        for field_name in _REQUIRED_FIELDS:
            value = getattr(doc, field_name, None)
            if value is None:
                return False, f"missing required field: {field_name}"
            if isinstance(value, str) and value == "":
                return False, f"missing required field: {field_name}"

        # Rule 2: sensitivity class must be Public.
        if doc.sensitivity_class != "Public":
            return False, f"sensitivity_class not Public: {doc.sensitivity_class}"

        # Rule 3: redistribution must be explicitly allowed.
        if doc.redistribution_allowed is not True:
            return False, "redistribution_allowed is not True"

        # Rule 4: license must be in the allowlist.
        if doc.license not in self.license_allowlist:
            return False, f"license not in allowlist: {doc.license}"

        return True, None

    def refusal(self, doc: SourceDoc, reason: str) -> ComplianceRefusal:
        """Build a ``ComplianceRefusal`` log record for ``doc``."""
        return ComplianceRefusal(
            canonical_id=doc.canonical_id or "",
            reason=reason,
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
