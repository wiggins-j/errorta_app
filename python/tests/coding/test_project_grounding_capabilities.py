from types import ModuleType

from errorta_project_grounding.capabilities import probe_aiar_grounding_capabilities


def test_probe_reports_absent_aiar() -> None:
    def importer(name: str) -> ModuleType:
        raise ModuleNotFoundError(name)

    caps = probe_aiar_grounding_capabilities(importer=importer)

    assert caps.available is False
    assert caps.supports_metadata_filters is False
    assert "aiar import failed" in caps.notes


def test_probe_detects_current_style_aiar_surface() -> None:
    root = ModuleType("aiar")
    root.__version__ = "0.2.9"
    ingest = ModuleType("aiar.rag.ingest")
    ingest.Chunk = object
    ingest.evict_chunks = lambda *a, **k: None
    store = ModuleType("aiar.rag.store")
    store.add = lambda *a, **k: None
    store.create_instance = lambda *a, **k: None
    harness = ModuleType("aiar.harness.pipeline")
    harness.answer_prompt = lambda *a, **k: None
    modules = {
        "aiar": root,
        "aiar.rag.ingest": ingest,
        "aiar.rag.store": store,
        "aiar.harness.pipeline": harness,
    }

    caps = probe_aiar_grounding_capabilities(importer=modules.__getitem__)

    assert caps.available is True
    assert caps.version == "0.2.9"
    assert caps.supports_corpus_ids is True
    assert caps.supports_file_ingest is True
    assert caps.supports_incremental_refresh is True
    assert caps.supports_record_ingest is False


def test_probe_marks_filter_gap_explicitly() -> None:
    modules = {"aiar": ModuleType("aiar"), "aiar.rag.ingest": ModuleType("aiar.rag.ingest")}

    caps = probe_aiar_grounding_capabilities(importer=modules.__getitem__)

    assert caps.available is True
    assert caps.supports_metadata_filters is False


def test_probe_detects_filterable_query_signature() -> None:
    root = ModuleType("aiar")
    store = ModuleType("aiar.rag.store")

    def query(prompt: str, *, filters: dict):
        return []

    store.query = query
    modules = {"aiar": root, "aiar.rag.store": store}

    caps = probe_aiar_grounding_capabilities(importer=modules.__getitem__)

    assert caps.supports_metadata_filters is True
