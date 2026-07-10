"""BENCH-REAL-FAKE — phase-aware deterministic mock snapshot tests.

Hermetic: no network, no live judge. The phase-aware
``DeterministicMockClient`` is exercised end-to-end via
``BenchmarkRunner.orchestrate_run_with_before_after`` against the pinned
``welcome_v1.yaml`` seed. The pass-rate bands asserted here are the
snapshot ranges spelled out in the wedge-story spec.

These assertions intentionally bound the metric ranges rather than pin
exact numbers — the bucketing thresholds in the runner are tuned for
the welcome_v1 seed, and any future seed expansion must keep the
bands honest.
"""
from __future__ import annotations

from pathlib import Path

from errorta_benchmark.aggregator import BenchmarkAggregator
from errorta_benchmark.prompts import load_prompts_yaml
from errorta_benchmark.runner import (
    BenchmarkRunner,
    DeterministicMockClient,
    _is_hallucination_wedge,
)


_SEED = (
    Path(__file__).resolve().parent.parent
    / "errorta_benchmark"
    / "seeds"
    / "welcome_v1.yaml"
)


def _pass_rate(verdicts: list) -> float:
    if not verdicts:
        return 0.0
    return sum(1 for v in verdicts if v.rating == "pass") / len(verdicts)


def _primary(verdicts: list) -> list:
    return [v for v in verdicts if not v.is_paraphrase_re_run]


def _paraphrase(verdicts: list) -> list:
    return [v for v in verdicts if v.is_paraphrase_re_run]


def test_before_after_pass_rates_hit_snapshot_bands() -> None:
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    before, after = runner.orchestrate_run_with_before_after(prompts)

    # BEFORE
    pr_before_primary = _pass_rate(_primary(before))
    pr_before_para = _pass_rate(_paraphrase(before))
    assert 0.45 <= pr_before_primary <= 0.55, (
        f"before primary pass_rate {pr_before_primary} not in [0.45,0.55]"
    )
    assert pr_before_para < pr_before_primary, (
        f"paraphrase {pr_before_para} should degrade below primary "
        f"{pr_before_primary}"
    )

    # AFTER
    pr_after_primary = _pass_rate(_primary(after))
    pr_after_para = _pass_rate(_paraphrase(after))
    assert 0.65 <= pr_after_primary <= 0.75, (
        f"after primary pass_rate {pr_after_primary} not in [0.65,0.75]"
    )
    # After correction the wedge story flips: paraphrase >= primary.
    assert pr_after_para >= pr_after_primary, (
        f"after paraphrase {pr_after_para} should not regress under after "
        f"primary {pr_after_primary}"
    )


def test_paraphrase_delta_sign_flips_after_correction() -> None:
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    before, after = runner.orchestrate_run_with_before_after(prompts)

    agg_before = BenchmarkAggregator().aggregate(before)
    agg_after = BenchmarkAggregator().aggregate(after)
    assert agg_before.F024_paraphrase_delta is not None
    assert agg_after.F024_paraphrase_delta is not None
    assert agg_before.F024_paraphrase_delta < 0, (
        f"expected negative paraphrase delta before correction, got "
        f"{agg_before.F024_paraphrase_delta}"
    )
    assert agg_after.F024_paraphrase_delta > 0, (
        f"expected positive paraphrase delta after correction, got "
        f"{agg_after.F024_paraphrase_delta}"
    )


def test_before_after_delta_is_numeric_and_positive() -> None:
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    before, after = runner.orchestrate_run_with_before_after(prompts)

    agg = BenchmarkAggregator().aggregate(after, before=before, after=after)
    assert agg.before_after_delta is not None, (
        "before_after_delta should be numeric after orchestrate_run_with_"
        "before_after"
    )
    assert agg.before_after_delta > 0.10, (
        f"before_after_delta {agg.before_after_delta} too small — wedge "
        "correction should lift primary pass_rate by ~0.20"
    )
    assert agg.matched_prompt_ids, "matched_prompt_ids should be populated"


def test_similar_match_count_positive_after_correction() -> None:
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    _, after = runner.orchestrate_run_with_before_after(prompts)
    agg = BenchmarkAggregator().aggregate(after)
    assert agg.f024_similar_match_count > 0, (
        "expected at least one similar grounding_match after correction"
    )
    assert agg.f024_similar_match_mean_similarity is not None
    assert 0.78 <= agg.f024_similar_match_mean_similarity <= 0.92, (
        "similarity should be drawn from [0.78, 0.92]"
    )


def test_median_score_is_not_quantized_to_bucket_set() -> None:
    """Continuous confidence ⇒ median_score outside {0, 0.25, 0.5, 0.75, 1.0}."""
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    _, after = runner.orchestrate_run_with_before_after(prompts)
    agg = BenchmarkAggregator().aggregate(after)
    quantized = {0.0, 0.25, 0.5, 0.75, 1.0}
    assert agg.median_score not in quantized, (
        f"median_score {agg.median_score} landed on the legacy bucket ladder"
    )


def test_hallucination_wedges_always_fail_on_primary_uncorrected() -> None:
    """The ~30% wedge subset must fail on primary unless corrected."""
    prompts = load_prompts_yaml(_SEED)
    wedges = sorted(p.id for p in prompts if _is_hallucination_wedge(p.id))
    # Composition sanity — welcome_v1 has 50 prompts so 30% ≈ 15–21.
    assert 12 <= len(wedges) <= 22, (
        f"unexpected wedge count {len(wedges)} for welcome_v1"
    )
    runner = BenchmarkRunner()
    before, _ = runner.orchestrate_run_with_before_after(prompts)
    by_id = {v.prompt_id: v for v in _primary(before)}
    for wid in wedges:
        assert by_id[wid].rating == "fail", (
            f"wedge {wid} should fail on primary before correction"
        )


def test_after_correction_flips_first_ten_wedges_to_pass() -> None:
    prompts = load_prompts_yaml(_SEED)
    wedges = sorted(p.id for p in prompts if _is_hallucination_wedge(p.id))
    expected_corrected = wedges[:10]
    runner = BenchmarkRunner()
    _, after = runner.orchestrate_run_with_before_after(prompts)
    primary_by_id = {v.prompt_id: v for v in _primary(after)}
    para_by_id = {v.prompt_id: v for v in _paraphrase(after)}
    for cid in expected_corrected:
        assert primary_by_id[cid].rating == "pass", (
            f"corrected wedge {cid} should pass on after-primary"
        )
        assert para_by_id[cid].rating == "pass", (
            f"corrected wedge {cid} should pass on after-paraphrase"
        )


def test_grounding_match_similar_block_only_on_after_paraphrase() -> None:
    prompts = load_prompts_yaml(_SEED)
    runner = BenchmarkRunner()
    _, after = runner.orchestrate_run_with_before_after(prompts)
    # No similar blocks on after-primary verdicts (the spec restricts
    # synthetic similar blocks to the paraphrase phase).
    for v in _primary(after):
        assert v.grounding_match_kind != "similar", (
            f"unexpected similar block on after-primary {v.prompt_id}"
        )
    # At least one paraphrase verdict carries a similar block with
    # similarity in the documented band.
    sim_paras = [
        v for v in _paraphrase(after) if v.grounding_match_kind == "similar"
    ]
    assert sim_paras
    for v in sim_paras:
        assert v.grounding_match_similarity is not None
        assert 0.78 <= v.grounding_match_similarity <= 0.92


def test_phase_aware_mock_yields_different_ratings_across_phases() -> None:
    """Across the seed, at least one prompt must produce >=2 distinct ratings
    over the four phases. A collapse to one rating everywhere would mean
    the phase channel is dead."""
    prompts = load_prompts_yaml(_SEED)
    wedges = sorted(p.id for p in prompts if _is_hallucination_wedge(p.id))
    corrected = wedges[:10]
    # Use a wedge as the test subject: it's guaranteed to differ between
    # primary (fail) and after-correction (pass).
    pid = corrected[0]
    client = DeterministicMockClient(corrected_ids=corrected, similar_ids=[])

    seen: set[str] = set()
    for phase in (
        "primary",
        "paraphrase",
        "after_correction_primary",
        "after_correction_paraphrase",
    ):
        resp = client.post(
            "/judge/verdict",
            json={
                "prompt": "ignored",
                "_mock_prompt_id": pid,
                "_mock_phase": phase,
            },
        )
        body = resp.json()
        seen.add(body["verdict"]["rating"])
        # Continuous confidence field is present in the mock body.
        assert "confidence" in body["verdict"]
        c = body["verdict"]["confidence"]
        assert 0.0 < c < 1.0

    assert seen == {"fail", "pass"}, (
        f"wedge prompt should fail pre-correction and pass post-correction; "
        f"got {seen}"
    )
