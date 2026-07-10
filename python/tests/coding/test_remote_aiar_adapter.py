"""RemoteAiarCorpusAdapter — tested against the AIAR remote-ingest spec contract.

The real AIAR endpoints don't exist yet (built in parallel), so we drive the
adapter with an injected httpx.MockTransport implementing
docs/specs/AIAR-remote-corpus-ingest-api.md.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from errorta_project_grounding.adapter import (
    ProjectGroundingError,
    UnsupportedGroundingOperation,
)
from errorta_project_grounding.remote_adapter import (
    RemoteAiarConfig,
    RemoteAiarCorpusAdapter,
    remote_aiar_config,
)

CFG = RemoteAiarConfig(base_url="http://127.0.0.1:8766", token="t0ken")
CFG_NO_TOKEN = RemoteAiarConfig(base_url="http://127.0.0.1:8766", token=None)


def _mock(handler):
    return httpx.MockTransport(handler)


def _adapter(handler, cfg=CFG) -> tuple[RemoteAiarCorpusAdapter, list]:
    recorded: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return handler(request)
    return RemoteAiarCorpusAdapter(cfg, transport=_mock(wrapped)), recorded


# --- config gating ----------------------------------------------------------


def test_config_is_none_when_url_unset(monkeypatch, tmp_errorta_home) -> None:
    monkeypatch.delenv("ERRORTA_AIAR_REMOTE_URL", raising=False)
    assert remote_aiar_config() is None


def test_config_reads_env(monkeypatch, tmp_errorta_home) -> None:
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_URL", "http://127.0.0.1:8766/")
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_TOKEN", "abc")
    cfg = remote_aiar_config()
    assert cfg and cfg.base_url == "http://127.0.0.1:8766" and cfg.token == "abc"


def test_default_adapter_selects_remote_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("ERRORTA_AIAR_REMOTE_URL", "http://127.0.0.1:8766")
    from errorta_project_grounding.adapter import default_project_grounding_adapter
    assert isinstance(default_project_grounding_adapter(), RemoteAiarCorpusAdapter)


# --- capabilities -----------------------------------------------------------


def test_capabilities_from_healthz() -> None:
    def h(req):
        assert req.url.path == "/healthz"
        return httpx.Response(200, json={"rag": {"store_ready": True, "embedder_ready": True,
                                                  "embedding_model": "all-MiniLM-L6-v2"}})
    a, _ = _adapter(h)
    caps = a.capabilities()
    assert caps.available and caps.source == "remote" and caps.local_only_embedding is False


def test_capabilities_fail_closed_when_unreachable() -> None:
    def h(req):
        raise httpx.ConnectError("no route")
    a, _ = _adapter(h)
    caps = a.capabilities()
    assert caps.available is False and "unreachable" in caps.source


# --- instance management + ingest ------------------------------------------


def test_ensure_instance_posts_name() -> None:
    def h(req):
        assert req.method == "POST" and req.url.path == "/instances"
        assert json.loads(req.content)["name"] == "proj-corpus"
        return httpx.Response(200, json={"instance": "proj-corpus", "status": "draft"})
    a, rec = _adapter(h)
    a.ensure_instance("proj-corpus", display_name="Proj")
    assert rec[0].headers["authorization"] == "Bearer t0ken"  # token sent


def _ingest_handler(captured, *, status="done", chunks_added=1, duplicates=0, errors=None):
    """Path-aware mock: POST /documents -> 202 {job_id}; GET ingest-jobs -> job."""
    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path.endswith("/documents"):
            captured.update(json.loads(req.content))
            return httpx.Response(202, json={"job_id": "job-1", "accepted": 1,
                                             "instance": "proj"})
        if req.method == "GET" and "/ingest-jobs/" in req.url.path:
            return httpx.Response(200, json={"status": status, "documents_total": 1,
                "chunks_added": chunks_added, "duplicates": duplicates,
                "errors": errors or []})
        return httpx.Response(200, json={})
    return h


def test_ingest_record_sends_document_and_confirms_storage() -> None:
    captured = {}
    a, _ = _adapter(_ingest_handler(captured))
    ref = a.ingest_record(corpus_id="proj", content="the answer is 42",
                          metadata={"title": "note"})
    doc = captured["documents"][0]
    assert doc["doc_id"] == hashlib.sha256(b"the answer is 42").hexdigest()
    assert doc["text"] == "the answer is 42"
    assert "vector" not in json.dumps(captured) and "embedding" not in json.dumps(captured)
    assert ref.record_id == "job-1" and ref.metadata["chunks_added"] == 1


def test_ingest_record_source_is_content_derived_and_unique() -> None:
    cap_a, cap_b = {}, {}
    a, _ = _adapter(_ingest_handler(cap_a))
    a.ingest_record(corpus_id="proj", content="alpha", metadata={})
    b, _ = _adapter(_ingest_handler(cap_b))
    b.ingest_record(corpus_id="proj", content="beta", metadata={})
    assert cap_a["documents"][0]["source"] != cap_b["documents"][0]["source"]  # no collision
    # caller-supplied stable source wins
    cap_c = {}
    c, _ = _adapter(_ingest_handler(cap_c))
    c.ingest_record(corpus_id="proj", content="x", metadata={"source": "ticket-7"})
    assert cap_c["documents"][0]["source"] == "ticket-7"


def test_ingest_file_extracts_then_posts_text(tmp_path: Path) -> None:
    f = tmp_path / "readme.md"
    f.write_text("# Title\n\nCalculator adds two numbers.\n", encoding="utf-8")
    captured = {}
    a, _ = _adapter(_ingest_handler(captured))
    a.ingest_file(corpus_id="proj", path=f, metadata={})
    doc = captured["documents"][0]
    assert "Calculator adds two numbers" in doc["text"]
    assert "pages" not in doc  # non-paged format -> flat text, no page spans


def test_ingest_file_sends_pages_when_extractor_has_page_numbers(tmp_path, monkeypatch) -> None:
    # simulate a paged extractor (e.g. PDF: one chunk/page with meta.page_number)
    def fake_extractor(_path):
        return [{"text": "page one text", "meta": {"page_number": 1}},
                {"text": "page two text", "meta": {"page_number": 2}}]
    monkeypatch.setattr("errorta_extract.registry.get_extractor", lambda ext: fake_extractor)
    monkeypatch.setattr("errorta_extract.registry.supported_extensions", lambda: [".pdf"])
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    captured = {}
    a, _ = _adapter(_ingest_handler(captured))
    a.ingest_file(corpus_id="proj", path=f, metadata={})
    doc = captured["documents"][0]
    assert doc["pages"] == [{"page": 1, "text": "page one text"},
                            {"page": 2, "text": "page two text"}]
    assert "page one text" in doc["text"] and "page two text" in doc["text"]


# --- review finding: ingest confirms storage (no silent partial success) ----


def test_ingest_fails_closed_on_failed_job() -> None:
    a, _ = _adapter(_ingest_handler({}, status="failed", chunks_added=0,
                                    errors=["embed error"]))
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="x", metadata={})


def test_ingest_fails_closed_when_nothing_stored() -> None:
    a, _ = _adapter(_ingest_handler({}, status="done", chunks_added=0, duplicates=0))
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="x", metadata={})


def test_ingest_idempotent_reingest_is_success() -> None:
    # done + chunks_added=0 + duplicates>0 == already ingested, NOT a failure
    a, _ = _adapter(_ingest_handler({}, status="done", chunks_added=0, duplicates=1))
    ref = a.ingest_record(corpus_id="proj", content="x", metadata={})
    assert ref.metadata["duplicates"] == 1


def test_ingest_fails_closed_when_no_job_id() -> None:
    def h(req):
        if req.url.path.endswith("/documents"):
            return httpx.Response(202, json={"accepted": 1})  # no job_id
        return httpx.Response(200, json={})
    a, _ = _adapter(h)
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="x", metadata={})


# --- security: nothing secret/denied leaves the machine ---------------------


def test_secret_content_refused_without_any_http_call() -> None:
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(202, json={})
    a, _ = _adapter(h)
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj",
                        content="token sk-ant-api03-AAAAAAAAAAAAAAAAAAAA", metadata={})
    assert calls == []  # never hit the network


def test_sensitive_path_refused(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SECRET=x\n", encoding="utf-8")
    a, _ = _adapter(lambda req: httpx.Response(202, json={}))
    with pytest.raises(ProjectGroundingError):
        a.ingest_file(corpus_id="proj", path=env, metadata={})


# --- fail-closed on auth / readiness ---------------------------------------


@pytest.mark.parametrize("status,frag", [(401, "token"), (503, "not ready")])
def test_http_errors_fail_closed(status, frag) -> None:
    a, _ = _adapter(lambda req: httpx.Response(status, json={}))
    with pytest.raises(ProjectGroundingError) as ei:
        a.ensure_instance("proj")
    assert frag in str(ei.value)


# --- retrieval --------------------------------------------------------------


def test_retrieve_filters_fail_closed() -> None:
    a, calls = _adapter(lambda req: httpx.Response(200, json={}))
    with pytest.raises(UnsupportedGroundingOperation):
        a.retrieve(corpus_id="proj", query="q", top_k=3, filters={"path": "a.py"})


# --- review finding: writes fail closed without a token --------------------


def test_writes_fail_closed_without_token() -> None:
    calls = []
    a, _ = _adapter(lambda req: (calls.append(req), httpx.Response(202, json={}))[1],
                    cfg=CFG_NO_TOKEN)
    for op in (lambda: a.ensure_instance("proj"),
               lambda: a.publish("proj"),
               lambda: a.ingest_record(corpus_id="proj", content="x", metadata={})):
        with pytest.raises(ProjectGroundingError):
            op()
    assert calls == []  # nothing transmitted unauthenticated


def test_reads_allowed_without_token() -> None:
    a, _ = _adapter(lambda req: httpx.Response(200, json={"rag": {"store_ready": True,
                    "embedder_ready": True}}), cfg=CFG_NO_TOKEN)
    assert a.capabilities().available is True  # /healthz needs no token


# --- review finding: metadata screened before egress -----------------------


def test_secret_in_metadata_refused_without_http_call() -> None:
    calls = []
    a, _ = _adapter(lambda req: (calls.append(req), httpx.Response(202, json={}))[1])
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="ok",
                        metadata={"note": "key sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"})
    assert calls == []


def test_vector_like_metadata_refused() -> None:
    calls = []
    a, _ = _adapter(lambda req: (calls.append(req), httpx.Response(202, json={}))[1])
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="ok",
                        metadata={"embedding": [0.01 * i for i in range(384)]})
    assert calls == []


def test_nested_vector_like_metadata_refused() -> None:
    calls = []
    a, _ = _adapter(lambda req: (calls.append(req), httpx.Response(202, json={}))[1])
    with pytest.raises(ProjectGroundingError):
        a.ingest_record(corpus_id="proj", content="ok",
                        metadata={"payload": {"embedding": [0.01 * i for i in range(384)]}})
    assert calls == []


# --- review finding: filename, not absolute path, as provenance ------------


def test_ingest_file_source_is_filename_not_absolute_path(tmp_path: Path) -> None:
    f = tmp_path / "secret-home-layout" / "notes.md"
    f.parent.mkdir()
    f.write_text("hello world\n", encoding="utf-8")
    captured = {}
    a, _ = _adapter(_ingest_handler(captured))
    a.ingest_file(corpus_id="proj", path=f, metadata={})
    doc = captured["documents"][0]
    assert doc["source"] == "notes.md"
    assert str(tmp_path) not in json.dumps(captured)  # no absolute path leaked


def test_ingest_file_source_uses_relative_path_metadata(tmp_path: Path) -> None:
    first = tmp_path / "docs" / "readme.md"
    second = tmp_path / "src" / "readme.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("docs readme\n", encoding="utf-8")
    second.write_text("src readme\n", encoding="utf-8")
    cap_a, cap_b = {}, {}
    a, _ = _adapter(_ingest_handler(cap_a))
    a.ingest_file(corpus_id="proj", path=first, metadata={"path": "docs/readme.md"})
    b, _ = _adapter(_ingest_handler(cap_b))
    b.ingest_file(corpus_id="proj", path=second, metadata={"path": "src/readme.md"})
    assert cap_a["documents"][0]["source"] == "docs/readme.md"
    assert cap_b["documents"][0]["source"] == "src/readme.md"
    assert cap_a["documents"][0]["source"] != cap_b["documents"][0]["source"]


def test_absolute_metadata_source_is_reduced_to_filename(tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("hello world\n", encoding="utf-8")
    captured = {}
    a, _ = _adapter(_ingest_handler(captured))
    a.ingest_file(corpus_id="proj", path=f, metadata={"source": str(tmp_path / "notes.md")})
    doc = captured["documents"][0]
    assert doc["source"] == "notes.md"
    assert str(tmp_path) not in doc["source"]


# --- review finding: capabilities don't overstate ingest support -----------


def test_capabilities_ingest_false_when_marker_absent() -> None:
    # a healthy but query-only AIAR (no remote_ingest marker)
    a, _ = _adapter(lambda req: httpx.Response(200, json={"rag": {
        "store_ready": True, "embedder_ready": True}}))
    caps = a.capabilities()
    assert caps.available is True
    assert caps.supports_file_ingest is False and caps.supports_record_ingest is False


def test_capabilities_ingest_true_when_marker_present() -> None:
    a, _ = _adapter(lambda req: httpx.Response(200, json={
        "remote_ingest": True, "rag": {"store_ready": True, "embedder_ready": True}}))
    caps = a.capabilities()
    assert caps.supports_file_ingest is True and caps.supports_record_ingest is True


# --- pure-retrieve endpoint (AIAR >= 0.2.3, GET /instances/{id}/retrieve) ----


def _healthz(pure_retrieve: bool) -> dict:
    return {"ok": True, "remote_ingest": True, "pure_retrieve": pure_retrieve,
            "rag": {"store_ready": True, "embedder_ready": True}}


def test_retrieve_prefers_pure_retrieve_when_advertised() -> None:
    paths: list[str] = []

    def h(req):
        paths.append(req.url.path)
        if req.url.path == "/healthz":
            return httpx.Response(200, json=_healthz(True))
        assert req.url.path == "/instances/proj/retrieve"
        assert req.url.params["q"] == "how to add" and req.url.params["k"] == "5"
        return httpx.Response(200, json={
            "instance": "proj", "query": "how to add", "k": 5,
            "score_kind": "cosine_similarity", "score_order": "desc", "count": 2,
            "hits": [
                {"chunk_id": "c1", "text": "calc.add(a,b)", "source": "calc.py",
                 "title": "calc", "score": 0.91, "chunk_index": 3,
                 "category": "general", "page_span": [1, 1]},
                {"chunk_id": "c2", "text": "returns the sum", "score": 0.80},
                {"chunk_id": "c3", "text": ""},  # empty text -> dropped
            ]})
    a, _ = _adapter(h)
    hits = a.retrieve(corpus_id="proj", query="how to add", top_k=5)
    # NO /services/prompt call — the pure path was used (no generation model)
    assert "/services/prompt" not in paths
    assert len(hits) == 2                                    # empty-text hit dropped
    assert hits[0].content == "calc.add(a,b)" and hits[0].chunk_id == "c1"
    assert hits[0].score == 0.91                             # real per-chunk score
    assert hits[0].metadata["source"] == "calc.py"
    assert hits[0].metadata["page_span"] == [1, 1]
    assert hits[0].metadata["score_kind"] == "cosine_similarity"


def test_pure_retrieve_probe_is_cached() -> None:
    calls: list[str] = []

    def h(req):
        calls.append(req.url.path)
        if req.url.path == "/healthz":
            return httpx.Response(200, json=_healthz(True))
        return httpx.Response(200, json={"instance": "p", "hits": [
            {"chunk_id": "x", "text": "t"}]})
    a, _ = _adapter(h)
    a.retrieve(corpus_id="p", query="a", top_k=2)
    a.retrieve(corpus_id="p", query="b", top_k=2)
    assert calls.count("/healthz") == 1                      # probed once, then cached


def test_retrieve_falls_back_to_services_prompt_without_marker() -> None:
    # a query-only AIAR (no pure_retrieve marker) -> legacy /services/prompt path
    def h(req):
        if req.url.path == "/healthz":
            return httpx.Response(200, json=_healthz(False))
        assert req.url.path == "/services/prompt"
        assert json.loads(req.content)["instance"] == "proj"
        return httpx.Response(200, json={"answer": "...", "retrieval": {"chunks": [
            {"text": "calc.add(a,b)", "chunk_id": "c1", "score": 0.9},
            {"content": "no id", "score": 0.5},
        ]}})
    a, _ = _adapter(h)
    hits = a.retrieve(corpus_id="proj", query="how to add", top_k=5)
    assert hits[0].content == "calc.add(a,b)"
    assert hits[0].chunk_id == "c1" and hits[0].score == 0.9
    assert len(hits) == 2


def test_fallback_sends_service_name_and_maps_grounded_answer() -> None:
    # live AIAR /services/prompt contract (fallback): requires service_name;
    # returns a GROUNDED ANSWER (not chunks). Represent it as a single hit.
    captured = {}

    def h(req):
        if req.url.path == "/healthz":
            return httpx.Response(200, json=_healthz(False))
        captured.update(json.loads(req.content))
        return httpx.Response(200, json={"answer": "divide raises ValueError",
                                         "grounded": True, "instance": "proj",
                                         "model": "qwen3.5:9b", "call_id": "cid1"})
    a, _ = _adapter(h)
    hits = a.retrieve(corpus_id="proj", query="zero?", top_k=4)
    assert captured["service_name"] and captured["instance"] == "proj"
    assert captured["rag"] is True and captured["judge"] is False
    assert hits[0].content == "divide raises ValueError"
    assert hits[0].chunk_id == "cid1" and hits[0].metadata["grounded"] is True


def test_fallback_ungrounded_answer_yields_no_hits() -> None:
    # an ungrounded answer is the model's general knowledge — never corpus evidence
    def h(req):
        if req.url.path == "/healthz":
            return httpx.Response(200, json=_healthz(False))
        return httpx.Response(200, json={"answer": "general fact", "grounded": False})
    a, _ = _adapter(h)
    assert a.retrieve(corpus_id="proj", query="q", top_k=3) == []


def test_pure_retrieve_probe_failure_falls_back() -> None:
    # if /healthz can't be read, default to the legacy path (fail closed, never
    # 404 on a route the old server lacks)
    def h(req):
        if req.url.path == "/healthz":
            return httpx.Response(503, json={})
        assert req.url.path == "/services/prompt"
        return httpx.Response(200, json={"answer": "x", "grounded": True,
                                         "instance": "proj", "call_id": "c"})
    a, _ = _adapter(h)
    assert a.retrieve(corpus_id="proj", query="q", top_k=2)[0].chunk_id == "c"
