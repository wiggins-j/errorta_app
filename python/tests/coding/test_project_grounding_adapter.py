from errorta_project_grounding.adapter import (
    AiarProjectGroundingAdapter,
    FallbackProjectGroundingAdapter,
    UnsupportedGroundingOperation,
)
from errorta_project_grounding.capabilities import AiarGroundingCapabilities


def _caps(**overrides):
    data = dict(
        available=True,
        version="x",
        source="test",
        supports_corpus_ids=True,
        supports_file_ingest=True,
        supports_record_ingest=False,
        supports_metadata_filters=False,
        supports_provenance_metadata=True,
        supports_incremental_refresh=False,
        supports_supersession=False,
        supports_export_import=False,
        local_only_embedding=True,
        notes=(),
    )
    data.update(overrides)
    return AiarGroundingCapabilities(**data)


def test_fallback_fails_closed() -> None:
    adapter = FallbackProjectGroundingAdapter(_caps(available=False))

    try:
        adapter.retrieve(corpus_id="c", query="q", top_k=3)
    except UnsupportedGroundingOperation as exc:
        assert "unavailable" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported operation")


def test_aiar_adapter_refuses_silent_filter_drop() -> None:
    adapter = AiarProjectGroundingAdapter(_caps(supports_metadata_filters=False))

    try:
        adapter.retrieve(corpus_id="c", query="q", top_k=3, filters={"authority": "durable_truth"})
    except UnsupportedGroundingOperation as exc:
        assert "metadata filters" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported operation")


def test_aiar_adapter_refuses_record_ingest_without_support() -> None:
    adapter = AiarProjectGroundingAdapter(_caps(supports_record_ingest=False))

    try:
        adapter.ingest_record(corpus_id="c", content="fact", metadata={})
    except UnsupportedGroundingOperation as exc:
        assert "record ingest" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected unsupported operation")


def test_aiar_adapter_retrieve_handles_query_result_without_source(monkeypatch) -> None:
    from errorta_query.models import QueryResult
    from errorta_query import pipeline as pipeline_mod

    class _Pipeline:
        def query(self, *, prompt, corpus_ids, top_k):
            assert prompt == "how to add"
            assert corpus_ids == ["proj"]
            assert top_k == 3
            return [
                QueryResult(
                    content="def add(a, b): return a + b",
                    corpus_id="proj",
                    chunk_id="ch1",
                    citation_id="ct1",
                    score=0.91,
                    tokens=7,
                )
            ]

    monkeypatch.setattr(pipeline_mod, "default_pipeline", lambda: _Pipeline())
    adapter = AiarProjectGroundingAdapter(_caps())

    hits = adapter.retrieve(corpus_id="proj", query="how to add", top_k=3)

    assert len(hits) == 1
    assert hits[0].metadata == {"citation_id": "ct1", "tokens": 7, "source": None}


def test_aiar_adapter_ingests_only_requested_file(
    tmp_path,
    tmp_errorta_home,
    isolated_manifest_locks,
) -> None:
    from errorta_corpus.manifest import load_manifest

    wanted = tmp_path / "wanted.md"
    unwanted = tmp_path / "unwanted.md"
    wanted.write_text("# wanted\n", encoding="utf-8")
    unwanted.write_text("# unwanted\n", encoding="utf-8")
    adapter = AiarProjectGroundingAdapter(_caps(supports_file_ingest=True))

    ref = adapter.ingest_file(corpus_id="single", path=wanted, metadata={"source": "test"})

    files = load_manifest("single")
    assert ref.record_id in files
    assert {entry.original_path for entry in files.values()} == {str(wanted)}


def test_aiar_adapter_source_file_is_pipeline_extractable(
    tmp_path,
    tmp_errorta_home,
    isolated_manifest_locks,
    monkeypatch,
) -> None:
    from pathlib import Path

    from errorta_corpus.manifest import load_manifest
    from errorta_extract import text as text_extractor
    from errorta_extract.registry import get_extractor, supported_extensions

    source = tmp_path / "app.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "errorta_corpus.pipeline.enqueue",
        lambda corpus_id, file_id: queued.append((corpus_id, file_id)),
    )
    adapter = AiarProjectGroundingAdapter(_caps(supports_file_ingest=True))

    assert ".py" not in supported_extensions()
    assert get_extractor(".py") is text_extractor.extract
    ref = adapter.ingest_file(corpus_id="source", path=source, metadata={"source": "src/app.py"})

    assert queued == [("source", ref.record_id)]
    entry = load_manifest("source")[ref.record_id]
    chunks = get_extractor(entry.mime_ext)(Path(entry.copied_path))
    assert chunks[0]["text"] == "def add(a, b):\n    return a + b"
