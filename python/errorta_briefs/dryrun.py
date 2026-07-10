"""F008 dry-run projection helper for the /briefs/{id}/validate endpoint.

Given a connector instance and a source spec, fetch up to ``sample_limit``
candidate docs from the connector's first search page and run each through a
``ComplianceGate``. The runner is NOT invoked — this is a side-effect-free
projection used to show users "what would happen" before they commit a run.

Yields ``(SourceDoc, accept_ok, refusal_reason)`` tuples. ``refusal_reason`` is
``None`` on accept.
"""
from __future__ import annotations

from typing import Iterator

from .compliance import ComplianceGate
from .connector import SourceConnector, SourceDoc
from .schema import SourceSpec


def dry_run_sample_source(
    connector: SourceConnector,
    source_spec: SourceSpec,
    gate: ComplianceGate,
    sample_limit: int = 5,
) -> Iterator[tuple[SourceDoc, bool, str | None]]:
    """Sample up to ``sample_limit`` docs from ``connector`` and gate-check each.

    Parameters
    ----------
    connector:
        An already-instantiated ``SourceConnector``. The caller owns
        construction and any cleanup.
    source_spec:
        The brief ``SourceSpec`` for this connector. Currently unused beyond
        identification, but accepted so future projections can vary by spec.
    gate:
        A ``ComplianceGate`` to run each candidate against.
    sample_limit:
        Maximum number of candidates to inspect from search(page=0). Defaults
        to 5. Values <= 0 yield nothing.
    """
    _ = source_spec  # reserved for future use; keeps signature stable
    if sample_limit <= 0:
        return
    count = 0
    for doc in connector.search(page=0):
        if count >= sample_limit:
            break
        ok, reason = gate.accepts(doc)
        yield doc, ok, reason
        count += 1
