"""Tests for the F008b SourceConnector ABC and shared exceptions."""
from __future__ import annotations

from typing import Iterator

import pytest

from errorta_briefs.connector import (
    FatalError,
    RetryableError,
    SourceConnector,
    SourceDoc,
)


def test_source_connector_abc_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        SourceConnector({})  # type: ignore[abstract]


def test_source_connector_has_exactly_six_abstract_methods() -> None:
    expected = {
        "__init__",
        "search",
        "fetch",
        "canonical_id",
        "metadata",
        "status",
    }
    assert SourceConnector.__abstractmethods__ == expected


def test_partial_subclass_still_abstract() -> None:
    class HalfConnector(SourceConnector):
        def __init__(self, config: dict) -> None:  # noqa: D401
            self.config = config

        def search(self, page: int) -> Iterator[SourceDoc]:  # pragma: no cover
            return iter(())

        # Missing fetch/canonical_id/metadata/status -> still abstract.

    with pytest.raises(TypeError):
        HalfConnector({})  # type: ignore[abstract]


def _doc() -> SourceDoc:
    return SourceDoc(
        canonical_id="x:1",
        title="t",
        source_url="https://example.com/1",
        publication_date="2026-01-01",
        sensitivity_class="Public",
        redistribution_allowed=True,
        license="CC-BY",
    )


def test_minimal_concrete_subclass_instantiates_and_runs() -> None:
    class FakeConnector(SourceConnector):
        def __init__(self, config: dict) -> None:
            self.config = config

        def search(self, page: int) -> Iterator[SourceDoc]:
            yield _doc()

        def fetch(self, doc: SourceDoc) -> bytes:
            return b"payload"

        def canonical_id(self, doc: SourceDoc) -> str:
            return doc.canonical_id

        def metadata(self, doc: SourceDoc) -> dict:
            return {"canonical_id": doc.canonical_id}

        def status(self) -> dict:
            return {"ok": True}

    c = FakeConnector({"foo": "bar"})
    assert c.config == {"foo": "bar"}
    docs = list(c.search(1))
    assert len(docs) == 1
    assert c.fetch(docs[0]) == b"payload"
    assert c.canonical_id(docs[0]) == "x:1"
    assert c.metadata(docs[0]) == {"canonical_id": "x:1"}
    assert c.status() == {"ok": True}


def test_retryable_error_carries_retry_after_hint() -> None:
    err = RetryableError("rate limited", retry_after_s=12.5)
    assert err.retry_after_s == 12.5
    assert "rate limited" in str(err)


def test_retryable_error_default_retry_after_is_none() -> None:
    err = RetryableError("blip")
    assert err.retry_after_s is None


def test_fatal_error_is_plain_exception() -> None:
    err = FatalError("auth denied")
    assert isinstance(err, Exception)
    assert "auth denied" in str(err)


def test_source_doc_extra_defaults_to_empty_dict() -> None:
    d = _doc()
    assert d.extra == {}
    # Independent default per instance.
    d.extra["k"] = "v"
    assert _doc().extra == {}
