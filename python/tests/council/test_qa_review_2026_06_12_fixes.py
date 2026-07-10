"""Regression tests for the QA architect review findings on PR #2 (2026-06-12).

- P1.1: route-prefix-aware destination_scope (anthropic.*/openai.*/google.*/custom.*
  must be classified as 'remote', not silently flattened to 'local').
- P1.2: user_prompt must outrank optional efficiency blocks (style/dialect/
  citation_index) so a fat dialect_instructions can't cascade-drop the question.
- P2.1: digest claim cites must surface in the citation appendix (render uses
  [c:..] marker syntax that citation_index_block already scans for).
- P2.2: TokenCalibrationStore wired into router actually applies to subsequent
  estimates (CalibratedEstimator wraps HeuristicEstimator with the stored ratio).
"""
from __future__ import annotations

from errorta_council.context.engine_adapter import _default_destination_scope_for
from errorta_council.context.packing import TokenPacker
from errorta_council.context.tokens import HeuristicEstimator


def _member(route_id: str, *, provider: str | None = None) -> dict:
    m: dict = {"gateway_route_id": route_id, "model": "x", "id": "m-x"}
    if provider is not None:
        m["provider"] = provider
    return m


# -- P1.1 ------------------------------------------------------------------


def test_p1_1_anthropic_route_classified_remote():
    assert _default_destination_scope_for(_member("anthropic.claude-haiku-4-5-20251001")) == "remote"


def test_p1_1_openai_route_classified_remote():
    assert _default_destination_scope_for(_member("openai.gpt-4o")) == "remote"


def test_p1_1_google_route_classified_remote():
    assert _default_destination_scope_for(_member("google.gemini-1.5-pro")) == "remote"


def test_p1_1_custom_route_classified_remote():
    assert _default_destination_scope_for(_member("custom.lmstudio.qwen3-coder")) == "remote"


def test_p1_1_local_route_classified_local():
    assert _default_destination_scope_for(_member("local.ollama.gemma3:27b")) == "local"


def test_p1_1_fake_route_classified_fake():
    assert _default_destination_scope_for(_member("fake.local.deterministic")) == "fake"


def test_p1_1_provider_field_overridden_by_route_prefix():
    # Old room snapshots may carry stale provider="local" with an F034
    # remote route. Route prefix must win to avoid payload_route_mismatch.
    member = _member("anthropic.claude-haiku-4-5-20251001", provider="local")
    assert _default_destination_scope_for(member) == "remote"


def test_p1_1_falls_back_to_provider_when_route_empty():
    # Guard against a degenerate empty-route case — no regression on the
    # legacy provider-field path.
    assert _default_destination_scope_for({"provider": "fake", "model": "x", "id": "m-x"}) == "fake"


# -- P1.2 ------------------------------------------------------------------


def _block(class_: str, content: str, *, tokens: int) -> dict:
    return {"class_": class_, "content": content, "content_sha256": class_, "tokens": tokens}


def test_p1_2_user_prompt_outranks_dialect_instructions_under_tight_budget():
    """A budget that fits only one of them must keep user_prompt and drop
    dialect_instructions."""
    packer = TokenPacker(max_input_tokens=15)
    packed = packer.pack([
        _block("dialect_instructions", "x" * 100, tokens=40),
        _block("user_prompt", "What's the capital of France?", tokens=10),
    ])
    kept_classes = {b["class_"] for b in packed.kept}
    omitted_classes = {b["class_"] for b in packed.omitted}
    assert "user_prompt" in kept_classes, "user_prompt must survive the tight-budget cascade"
    assert "dialect_instructions" in omitted_classes, "dialect_instructions must yield first"


def test_p1_2_user_prompt_outranks_style_instructions():
    packer = TokenPacker(max_input_tokens=15)
    packed = packer.pack([
        _block("style_instructions", "x" * 100, tokens=40),
        _block("user_prompt", "What's the capital of France?", tokens=10),
    ])
    kept_classes = {b["class_"] for b in packed.kept}
    assert "user_prompt" in kept_classes


def test_p1_2_user_prompt_outranks_citation_index():
    packer = TokenPacker(max_input_tokens=15)
    packed = packer.pack([
        _block("citation_index", "Citations: c1, c2 ...", tokens=40),
        _block("user_prompt", "Q?", tokens=10),
    ])
    kept_classes = {b["class_"] for b in packed.kept}
    assert "user_prompt" in kept_classes


def test_p1_2_task_instructions_still_outrank_user_prompt():
    """task_instructions stays at slot 0 — system constraints must always
    survive even if it costs the user_prompt. The packer's contract is
    'highest-priority survives', not 'user_prompt is unconditional'."""
    packer = TokenPacker(max_input_tokens=40)
    packed = packer.pack([
        _block("user_prompt", "Q?", tokens=10),
        _block("task_instructions", "x" * 100, tokens=40),
    ])
    kept_classes = {b["class_"] for b in packed.kept}
    assert "task_instructions" in kept_classes


# -- P2.1 ------------------------------------------------------------------


def test_p2_1_digest_render_emits_c_marker_syntax():
    """digest claim cites must use [c:id] syntax so CitationRegistry's alias
    scan picks them up. The prior 'cites c1,c2' format never matched."""
    from errorta_council.context.dialect.render import render_digest_v1

    digest = {
        "v": "digest_v1",
        "position": "Paris.",
        "claims": [{
            "id": "k1",
            "text": "Paris is the capital.",
            "cites": ["c1", "c2"],
            "confidence": "high",
        }],
    }
    rendered = render_digest_v1(digest, member_id="m-1", round_n=1)
    assert "[c:c1]" in rendered
    assert "[c:c2]" in rendered


def test_p2_1_citation_registry_alias_scan_catches_digest_cites(tmp_path):
    """End-to-end lock: rendered digest text must surface in the citation
    appendix when fed to CitationRegistry.aliases_in_text."""
    from errorta_council.context.citations import CitationRegistry, citation_index_block
    from errorta_council.context.dialect.render import render_digest_v1

    registry = CitationRegistry(path=tmp_path / "citations.json")
    registry.register(
        corpus_id="welcome",
        chunk_id=None,
        content_sha256="a" * 64,
        tokens=12,
        title_hint="primary source",
    )
    registry.register(
        corpus_id="welcome",
        chunk_id=None,
        content_sha256="b" * 64,
        tokens=8,
        title_hint="secondary source",
    )

    digest = {
        "v": "digest_v1",
        "position": "Paris.",
        "claims": [{"id": "k1", "text": "Paris.", "cites": ["c1", "c2"], "confidence": "high"}],
    }
    transcript_text = render_digest_v1(digest)
    aliases = registry.aliases_in_text(transcript_text)
    assert "c1" in aliases
    assert "c2" in aliases

    block = citation_index_block(registry, transcript_text)
    assert block is not None
    assert "c1" in block["content"]
    assert "c2" in block["content"]


# -- P2.2 ------------------------------------------------------------------


def test_p2_2_calibration_key_local_ollama():
    from errorta_council.context.router import _calibration_key_from_route

    assert _calibration_key_from_route("local.ollama.gemma3:27b") == ("ollama", "gemma3:27b")


def test_p2_2_calibration_key_remote_anthropic():
    from errorta_council.context.router import _calibration_key_from_route

    assert _calibration_key_from_route("anthropic.claude-haiku-4-5-20251001") == (
        "anthropic", "claude-haiku-4-5-20251001"
    )


def test_p2_2_calibration_key_fake():
    from errorta_council.context.router import _calibration_key_from_route

    assert _calibration_key_from_route("fake.local.deterministic") == ("fake", "local.deterministic")


def test_p2_2_calibration_key_empty():
    from errorta_council.context.router import _calibration_key_from_route

    assert _calibration_key_from_route("") == ("", "")


def test_p2_2_router_reads_stored_factor_into_estimator(tmp_path):
    """End-to-end: a non-1.0 factor on disk applies to subsequent build
    estimates via CalibratedEstimator wrap. Before this fix the store
    accepted writes but reads were never plumbed."""
    from errorta_council.context.router import ContextRouter
    from errorta_council.context.tokens import (
        CalibratedEstimator,
        CalibrationSample,
        HeuristicEstimator,
        TokenCalibrationStore,
    )

    store = TokenCalibrationStore(tmp_path / "factors.json")
    store.record(CalibrationSample(provider="ollama", model="gemma3:27b", ratio=1.5))

    class _FakeReq:
        gateway_route_id = "local.ollama.gemma3:27b"

    class _FakeRouter:
        _calibration_store = store
        _token_estimator = HeuristicEstimator()

        # Borrow the real resolver
        _resolve_calibrated_estimator = ContextRouter._resolve_calibrated_estimator

    router = _FakeRouter()
    estimator = router._resolve_calibrated_estimator(_FakeReq())
    assert isinstance(estimator, CalibratedEstimator)
    assert estimator.calibration_factor == 1.5

    # And the wrapped estimate is the base * factor (ceil)
    base = HeuristicEstimator().estimate("Hello world.", content_kind="prose")
    wrapped = estimator.estimate("Hello world.", content_kind="prose")
    assert wrapped >= int(base * 1.5) - 1  # ceil semantics


def test_p2_2_unknown_route_falls_back_to_base_estimator(tmp_path):
    """No calibration entry for an unseen route → return base estimator
    unwrapped (no spurious CalibratedEstimator wrap with factor=1.0)."""
    from errorta_council.context.router import ContextRouter
    from errorta_council.context.tokens import HeuristicEstimator, TokenCalibrationStore

    store = TokenCalibrationStore(tmp_path / "factors.json")

    class _FakeReq:
        gateway_route_id = "local.ollama.never-seen:1b"

    class _FakeRouter:
        _calibration_store = store
        _token_estimator = HeuristicEstimator()
        _resolve_calibrated_estimator = ContextRouter._resolve_calibrated_estimator

    router = _FakeRouter()
    estimator = router._resolve_calibrated_estimator(_FakeReq())
    assert isinstance(estimator, HeuristicEstimator)
