"""F024 unit tests — embeddings module + grounding similarity layer.

All tests are hermetic. Ollama is never called; ``httpx.Client`` is monkeypatched
to return deterministic payloads. ``tmp_errorta_home`` keeps the on-disk store
inside the test tmp dir.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from errorta_query import embeddings as emb
from errorta_query import grounding

# ---------------- cosine_similarity ----------------


def test_cosine_identical_vectors_is_one() -> None:
    v = [0.1, 0.2, 0.3, 0.4]
    assert emb.cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-9)


def test_cosine_orthogonal_vectors_is_zero() -> None:
    assert emb.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0, abs=1e-9)


def test_cosine_opposite_vectors_is_minus_one() -> None:
    assert emb.cosine_similarity([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0, abs=1e-9)


def test_cosine_zero_vector_guard_returns_zero() -> None:
    assert emb.cosine_similarity([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0
    assert emb.cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0


def test_cosine_mismatched_lengths_returns_zero() -> None:
    assert emb.cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0


def test_cosine_empty_returns_zero() -> None:
    assert emb.cosine_similarity([], []) == 0.0


# ---------------- EmbeddingStore ----------------


def test_store_append_and_iter_roundtrip(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore()
    store.append("sig-a", [0.1, 0.2, 0.3], "prompt a")
    store.append("sig-b", [0.4, 0.5, 0.6], "prompt b")

    records = list(store.iter_records())
    assert len(records) == 2
    assert records[0]["signature"] == "sig-a"
    assert records[0]["prompt_text"] == "prompt a"
    assert records[0]["embedding"] == [0.1, 0.2, 0.3]
    assert "created_at" in records[0]
    assert records[1]["signature"] == "sig-b"


def test_store_iter_missing_file_yields_nothing(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore(path=tmp_errorta_home / ".errorta" / "no-such-file.jsonl")
    assert list(store.iter_records()) == []


def test_store_lookup_above_threshold_hits(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore()
    store.append("sig-near", [1.0, 0.0, 0.0], "x")
    store.append("sig-far", [0.0, 1.0, 0.0], "y")

    matches = store.lookup_by_similarity([1.0, 0.01, 0.0], threshold=0.9)
    assert matches
    assert matches[0][0] == "sig-near"
    assert matches[0][1] > 0.9


def test_store_lookup_below_threshold_misses(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore()
    store.append("sig-far", [0.0, 1.0, 0.0], "y")
    matches = store.lookup_by_similarity([1.0, 0.0, 0.0], threshold=0.5)
    assert matches == []


def test_store_lookup_sorted_descending(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore()
    store.append("sig-mid", [1.0, 0.5, 0.0], "m")
    store.append("sig-best", [1.0, 0.01, 0.0], "b")
    store.append("sig-worst", [1.0, 0.9, 0.0], "w")
    matches = store.lookup_by_similarity([1.0, 0.0, 0.0], threshold=0.5)
    sims = [m[1] for m in matches]
    assert sims == sorted(sims, reverse=True)
    assert matches[0][0] == "sig-best"


def test_store_skips_torn_trailing_line(tmp_errorta_home: Path) -> None:
    store = emb.EmbeddingStore()
    store.append("sig-a", [0.1, 0.2], "prompt a")
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("{not-valid-json")
    records = list(store.iter_records())
    assert len(records) == 1
    assert records[0]["signature"] == "sig-a"


# ---------------- ollama_embed ----------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.last_url: str | None = None
        self.last_json: Any = None

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, json: Any = None) -> _FakeResponse:
        self.last_url = url
        self.last_json = json
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    def _factory(*args: Any, **kwargs: Any) -> _FakeClient:
        return fake

    monkeypatch.setattr(emb.httpx, "Client", _factory)


def test_ollama_embed_returns_floats(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(_FakeResponse(200, {"embedding": [0.1, 0.2, 0.3]}))
    _patch_httpx_client(monkeypatch, fake)

    out = emb.ollama_embed("hello", host="http://1.2.3.4:11434", model="nomic-embed-text")
    assert out == [0.1, 0.2, 0.3]
    assert fake.last_url == "http://1.2.3.4:11434/api/embeddings"
    assert fake.last_json == {"model": "nomic-embed-text", "prompt": "hello"}


def test_ollama_embed_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(_FakeResponse(200, {"embedding": [1.0]}))
    _patch_httpx_client(monkeypatch, fake)
    emb.ollama_embed("hi", host="http://1.2.3.4:11434/")
    assert fake.last_url == "http://1.2.3.4:11434/api/embeddings"


def test_ollama_embed_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(httpx.ConnectError("refused"))
    _patch_httpx_client(monkeypatch, fake)
    with pytest.raises(emb.EmbeddingUnavailable):
        emb.ollama_embed("x")


def test_ollama_embed_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(_FakeResponse(404, {"error": "model not found"}))
    _patch_httpx_client(monkeypatch, fake)
    with pytest.raises(emb.EmbeddingUnavailable):
        emb.ollama_embed("x")


def test_ollama_embed_raises_on_missing_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(_FakeResponse(200, {"not-embedding": []}))
    _patch_httpx_client(monkeypatch, fake)
    with pytest.raises(emb.EmbeddingUnavailable):
        emb.ollama_embed("x")


def test_ollama_embed_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(httpx.ReadTimeout("slow"))
    _patch_httpx_client(monkeypatch, fake)
    with pytest.raises(emb.EmbeddingUnavailable):
        emb.ollama_embed("x")


# ---------------- grounding.lookup_by_similarity ----------------


def test_lookup_by_similarity_returns_none_when_env_off(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERRORTA_GROUNDING_EMBEDDINGS", raising=False)
    # Even with data in the store, env-off should short-circuit.
    grounding.record("sig-x", "the correction")
    emb.EmbeddingStore().append("sig-x", [1.0, 0.0], "original prompt")
    assert grounding.lookup_by_similarity("paraphrased prompt") is None


def test_lookup_by_similarity_fails_open_on_embedding_unavailable(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")

    def _boom(*args: Any, **kwargs: Any) -> list[float]:
        raise emb.EmbeddingUnavailable("ollama down")

    monkeypatch.setattr(emb, "ollama_embed", _boom)
    # Also patch the lazy import inside grounding.
    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _boom)
    assert grounding.lookup_by_similarity("anything") is None


def test_lookup_by_similarity_returns_match(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")
    grounding.record("sig-known", "the canonical correction")
    emb.EmbeddingStore().append("sig-known", [1.0, 0.0, 0.0], "original")

    def _fixed(*args: Any, **kwargs: Any) -> list[float]:
        return [0.99, 0.05, 0.0]

    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _fixed)

    result = grounding.lookup_by_similarity("paraphrased")
    assert result is not None
    sig, correction, sim = result
    assert sig == "sig-known"
    assert correction == "the canonical correction"
    assert sim > 0.95


def test_lookup_by_similarity_returns_none_when_below_threshold(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")
    grounding.record("sig-known", "the canonical correction")
    emb.EmbeddingStore().append("sig-known", [1.0, 0.0, 0.0], "original")

    def _orthogonal(*args: Any, **kwargs: Any) -> list[float]:
        return [0.0, 1.0, 0.0]

    import errorta_query.embeddings as _e

    monkeypatch.setattr(_e, "ollama_embed", _orthogonal)

    assert grounding.lookup_by_similarity("totally different topic") is None


# ---------------- grounding.record_with_embedding ----------------


def test_record_with_embedding_skips_embedding_when_env_off(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ERRORTA_GROUNDING_EMBEDDINGS", raising=False)
    calls: list[Any] = []

    import errorta_query.embeddings as _e

    def _track(*args: Any, **kwargs: Any) -> list[float]:
        calls.append((args, kwargs))
        return [1.0]

    monkeypatch.setattr(_e, "ollama_embed", _track)

    grounding.record_with_embedding("sig-1", "correction", "prompt")
    assert grounding.lookup("sig-1") == "correction"
    assert calls == []
    assert not emb.embeddings_path().exists()


def test_record_with_embedding_writes_embedding_when_env_on(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")

    import errorta_query.embeddings as _e

    def _fixed(*args: Any, **kwargs: Any) -> list[float]:
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(_e, "ollama_embed", _fixed)

    grounding.record_with_embedding("sig-1", "correction", "prompt text")
    assert grounding.lookup("sig-1") == "correction"
    records = list(emb.EmbeddingStore().iter_records())
    assert len(records) == 1
    assert records[0]["signature"] == "sig-1"
    assert records[0]["embedding"] == [0.1, 0.2, 0.3]
    assert records[0]["prompt_text"] == "prompt text"


def test_record_with_embedding_fails_open_on_embedding_error(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ERRORTA_GROUNDING_EMBEDDINGS", "1")

    import errorta_query.embeddings as _e

    def _boom(*args: Any, **kwargs: Any) -> list[float]:
        raise emb.EmbeddingUnavailable("ollama down")

    monkeypatch.setattr(_e, "ollama_embed", _boom)

    grounding.record_with_embedding("sig-2", "correction", "prompt text")
    # SHA-256 path must still succeed.
    assert grounding.lookup("sig-2") == "correction"
    assert not emb.embeddings_path().exists()
