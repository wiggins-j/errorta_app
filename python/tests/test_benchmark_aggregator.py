"""F-DEMO-01 aggregator — pure-logic tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_benchmark.aggregator import AggregationResult, BenchmarkAggregator
from errorta_benchmark.prompts import load_prompts_yaml
from errorta_benchmark.report import render_markdown
from errorta_benchmark.runner import RecordedVerdict


def _v(
    pid: str,
    rating: str,
    *,
    paraphrase: bool = False,
) -> RecordedVerdict:
    score = {"pass": 1.0, "partial": 0.5, "fail": 0.0, "uncertain": 0.5}.get(rating, 0.0)
    return RecordedVerdict(
        prompt_id=pid,
        prompt_text=pid,
        is_paraphrase_re_run=paraphrase,
        rating=rating,
        score=score,
    )


def test_empty_input_returns_sensible_defaults() -> None:
    agg = BenchmarkAggregator().aggregate([])
    assert isinstance(agg, AggregationResult)
    assert agg.total == 0
    assert agg.pass_rate == 0.0
    assert agg.median_score == 0.0
    assert agg.rating_counts == {}
    assert agg.before_after_delta is None
    assert agg.F024_paraphrase_delta is None
    assert agg.matched_prompt_ids == []


def test_median_across_mixed_ratings() -> None:
    verdicts = [
        _v("a", "pass"),       # 1.0
        _v("b", "uncertain"),  # 0.5
        _v("c", "fail"),       # 0.0
        _v("d", "pass"),       # 1.0
        _v("e", "uncertain"),  # 0.5
    ]
    agg = BenchmarkAggregator().aggregate(verdicts)
    # sorted scores: 0.0, 0.5, 0.5, 1.0, 1.0 → median 0.5
    assert agg.median_score == 0.5
    # pass_rate: 2 / 5
    assert agg.pass_rate == pytest.approx(0.4)
    assert agg.rating_counts == {"pass": 2, "uncertain": 2, "fail": 1}


def test_partial_counts_as_half_score_but_not_pass() -> None:
    agg = BenchmarkAggregator().aggregate([_v("a", "partial"), _v("b", "pass")])
    assert agg.median_score == 0.75
    assert agg.pass_rate == pytest.approx(0.5)
    assert agg.rating_counts == {"partial": 1, "pass": 1}


def test_median_even_count_averages_middle_two() -> None:
    verdicts = [_v("a", "pass"), _v("b", "fail")]
    agg = BenchmarkAggregator().aggregate(verdicts)
    assert agg.median_score == 0.5


def test_before_after_delta_uses_matched_ids_only() -> None:
    before = [_v("a", "fail"), _v("b", "fail"), _v("c", "pass")]
    # 'c' not present in after, 'd' not in before — both excluded from delta
    after = [_v("a", "pass"), _v("b", "pass"), _v("d", "pass")]
    agg = BenchmarkAggregator().aggregate([], before=before, after=after)
    # matched ids = {a, b}; before pass_rate over a,b = 0/2; after = 2/2; delta = 1.0
    assert agg.matched_prompt_ids == ["a", "b"]
    assert agg.before_after_delta == pytest.approx(1.0)


def test_before_after_delta_none_when_no_intersection() -> None:
    before = [_v("a", "pass")]
    after = [_v("b", "pass")]
    agg = BenchmarkAggregator().aggregate([], before=before, after=after)
    assert agg.before_after_delta is None
    assert agg.matched_prompt_ids == []


def test_paraphrase_delta_none_when_no_paraphrase_entries() -> None:
    verdicts = [_v("a", "pass"), _v("b", "fail")]
    agg = BenchmarkAggregator().aggregate(verdicts)
    assert agg.F024_paraphrase_delta is None


def test_paraphrase_delta_uses_matched_intersection() -> None:
    verdicts = [
        _v("a", "pass"),
        _v("b", "fail"),
        _v("c", "pass"),  # no paraphrase counterpart — excluded
        _v("a", "fail", paraphrase=True),
        _v("b", "pass", paraphrase=True),
        _v("z", "pass", paraphrase=True),  # no primary — excluded
    ]
    agg = BenchmarkAggregator().aggregate(verdicts)
    # matched = {a, b}; primary pass_rate = 1/2 = 0.5; paraphrase = 1/2 = 0.5; delta 0.0
    assert agg.F024_paraphrase_delta == pytest.approx(0.0)


def test_render_markdown_is_deterministic_given_fixed_metadata() -> None:
    verdicts = [_v("a", "pass"), _v("b", "fail")]
    agg = BenchmarkAggregator().aggregate(verdicts)
    meta = {
        "title": "T",
        "seed": "welcome_v1",
        "generated_at": "2026-06-07T00:00:00Z",
        "notes": "scaffold",
    }
    a = render_markdown(agg, meta)
    b = render_markdown(agg, meta)
    assert a == b
    # The only timestamp present is the explicit generated_at value.
    assert "2026-06-07T00:00:00Z" in a
    # Headings are present.
    assert "# T" in a
    assert "## Rating counts" in a


def test_load_prompts_seed_file_has_ten_unique_entries() -> None:
    seed = Path(__file__).resolve().parents[1] / "errorta_benchmark" / "prompts" / "welcome_v1.yaml"
    prompts = load_prompts_yaml(seed)
    assert len(prompts) == 10
    assert len({p.id for p in prompts}) == 10
    for p in prompts:
        assert p.text.strip()
        assert p.paraphrase.strip()


# F-DEMO-01 Slice (b) Run metadata renderer tests ---------------------------


def test_render_markdown_omits_run_metadata_when_unset() -> None:
    """Legacy callers (no provenance fields) must NOT see a Run metadata
    subsection. Locks the byte-stability promise for the existing test."""
    agg = BenchmarkAggregator().aggregate([_v("a", "pass")])
    meta = {
        "title": "T",
        "seed": "welcome_v1",
        "generated_at": "2026-06-07T00:00:00Z",
    }
    out = render_markdown(agg, meta)
    assert "## Run metadata" not in out
    assert "judge_model" not in out
    assert "seed_sha256" not in out


def test_render_markdown_emits_run_metadata_when_set() -> None:
    """All four provenance fields land in a deterministic table when set."""
    agg = BenchmarkAggregator().aggregate([_v("a", "pass")])
    meta = {
        "title": "T",
        "seed": "welcome_v1",
        "generated_at": "2026-06-07T00:00:00Z",
        "judge_model": "llama3.1:8b",
        "ollama_version": "0.5.4",
        "aiar_pin_source": "editable",
        "seed_sha256": "deadbeef" * 8,
    }
    out = render_markdown(agg, meta)
    assert "## Run metadata" in out
    assert "| judge_model | `llama3.1:8b` |" in out
    assert "| ollama_version | `0.5.4` |" in out
    assert "| aiar_pin_source | `editable` |" in out
    assert "| seed_sha256 | `" + ("deadbeef" * 8) + "` |" in out
    # Run metadata subsection sits before Methodology so the provenance is
    # visible above the fold.
    assert out.index("## Run metadata") < out.index("## Methodology")


def test_seed_sha256_is_deterministic_and_hex(tmp_path: Path) -> None:
    """Lock the provenance hash shape — 64 hex chars, byte-deterministic."""
    from errorta_benchmark.__main__ import _seed_sha256

    seed = tmp_path / "seed.yaml"
    seed.write_bytes(b"- id: a\n  text: hi\n  paraphrase: hello\n")
    digest = _seed_sha256(seed)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert _seed_sha256(seed) == digest  # byte-stable


def test_render_markdown_emits_run_metadata_partial_subset() -> None:
    """Subset of provenance fields renders only the present rows."""
    agg = BenchmarkAggregator().aggregate([_v("a", "pass")])
    meta = {
        "title": "T",
        "seed": "welcome_v1",
        "generated_at": "2026-06-07T00:00:00Z",
        "judge_model": "llama3.1:8b",
    }
    out = render_markdown(agg, meta)
    assert "## Run metadata" in out
    assert "judge_model" in out
    assert "ollama_version" not in out
    assert "aiar_pin_source" not in out
    assert "seed_sha256" not in out


def test_load_prompts_yaml_rejects_duplicate_ids(tmp_path: Path) -> None:
    bad = tmp_path / "dupes.yaml"
    bad.write_text(
        "- id: a\n  text: x\n  paraphrase: y\n"
        "- id: a\n  text: x2\n  paraphrase: y2\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_prompts_yaml(bad)


def test_load_prompts_yaml_rejects_empty_text(tmp_path: Path) -> None:
    bad = tmp_path / "empty.yaml"
    bad.write_text(
        "- id: a\n  text: ''\n  paraphrase: y\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="text"):
        load_prompts_yaml(bad)


def test_load_prompts_yaml_rejects_empty_paraphrase(tmp_path: Path) -> None:
    bad = tmp_path / "empty_p.yaml"
    bad.write_text(
        "- id: a\n  text: x\n  paraphrase: ''\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="paraphrase"):
        load_prompts_yaml(bad)
