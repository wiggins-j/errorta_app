"""F143 / F143-01 Slice D — pure token-usage rollup over coding-ledger turns."""
from errorta_council.coding.usage_rollup import rollup_turns

_EMPTY_TOTAL = {
    "input": 0, "output": 0,
    "measured_input": 0, "measured_output": 0,
    "estimated_input": 0, "estimated_output": 0,
    "cache_read": 0, "cache_write": 0,
    "turns": 0, "measured_turns": 0, "partial_turns": 0,
    "estimated_turns": 0, "unreported_turns": 0,
    "coverage": {"measured_pct": 0, "estimated_pct": 0},
}


def _measured(inp, out, **extra):
    # A genuinely MEASURED turn: effective == measured (Slice C/D shape).
    u = {"measured": True, "provenance": "measured",
         "input_tokens": inp, "output_tokens": out,
         "measured_input": inp, "measured_output": out}
    u.update(extra)
    return {"usage": u}


def _estimated(inp, out, **extra):
    # A dark-provider turn: effective == estimate, no measured portion.
    u = {"measured": False, "provenance": "estimated",
         "input_tokens": inp, "output_tokens": out,
         "estimated_input": inp, "estimated_output": out}
    u.update(extra)
    return {"usage": u}


def _partial(*, measured_in, est_out, **extra):
    # measured input only; output filled from estimate. effective = measured_in + est_out.
    u = {"measured": True, "provenance": "measured_partial",
         "input_tokens": measured_in, "output_tokens": est_out,
         "measured_input": measured_in, "estimated_output": est_out}
    u.update(extra)
    return {"usage": u}


def test_empty_turns_yield_zeroed_total() -> None:
    r = rollup_turns([])
    assert r["by_member"] == {} and r["by_route"] == {} and r["by_role"] == {}
    assert r["total"] == _EMPTY_TOTAL


def test_sums_by_member_route_and_total() -> None:
    turns = [
        {"member_id": "m1", "model_assignment": {"route_id": "anthropic.sonnet"},
         **_measured(10, 5)},
        {"member_id": "m1", "model_assignment": {"route_id": "anthropic.sonnet"},
         **_measured(20, 8)},
        {"member_id": "m2", "model_assignment": {"route_id": "openai.gpt"},
         **_measured(100, 40, cache_read_input_tokens=7, cache_write_input_tokens=3)},
    ]
    r = rollup_turns(turns)
    assert r["total"]["input"] == 130 and r["total"]["output"] == 53
    assert r["total"]["cache_read"] == 7 and r["total"]["cache_write"] == 3
    assert r["total"]["turns"] == 3 and r["total"]["measured_turns"] == 3
    # all-measured → 100% coverage
    assert r["total"]["coverage"] == {"measured_pct": 100, "estimated_pct": 0}
    assert r["by_member"]["m1"]["input"] == 30 and r["by_member"]["m1"]["turns"] == 2
    assert r["by_member"]["m2"]["output"] == 40
    assert r["by_route"]["anthropic.sonnet"]["input"] == 30
    assert r["by_route"]["openai.gpt"]["input"] == 100


def test_effective_headline_mixes_measured_and_estimated_with_coverage() -> None:
    # 2 members / 2 routes / 2 roles, mixing measured + estimated + partial turns.
    turns = [
        # measured DEV on route A: eff 100/40, all measured.
        {"member_id": "m-dev", "role": "dev", "route_id": "rA", **_measured(100, 40)},
        # dark DEV on route A: eff 300/60, all estimated (the motivating dark turn).
        {"member_id": "m-dev", "role": "dev", "route_id": "rA", **_estimated(300, 60)},
        # partial REVIEWER on route B: measured input 50, estimated output 10.
        {"member_id": "m-rev", "role": "reviewer", "route_id": "rB",
         **_partial(measured_in=50, est_out=10)},
    ]
    r = rollup_turns(turns)
    t = r["total"]
    # Effective headline = measured + estimated portions.
    assert t["input"] == 100 + 300 + 50   # 450
    assert t["output"] == 40 + 60 + 10     # 110
    # Measured portion: measured input 100+50=150, measured output 40 only.
    assert t["measured_input"] == 150 and t["measured_output"] == 40
    # Estimated portion in the headline = effective - measured:
    #   input: 450 - 150 = 300 ; output: 110 - 40 = 70
    assert t["estimated_input"] == 300 and t["estimated_output"] == 70
    # Provenance counts partition the 3 turns.
    assert t["measured_turns"] == 1 and t["estimated_turns"] == 1
    assert t["partial_turns"] == 1 and t["unreported_turns"] == 0
    assert t["turns"] == 3
    # Coverage = measured share of headline tokens (150+40)/(450+110)=190/560≈34%.
    assert t["coverage"]["measured_pct"] == round(100 * 190 / 560)
    assert t["coverage"]["estimated_pct"] == 100 - t["coverage"]["measured_pct"]
    # by_role split.
    assert set(r["by_role"]) == {"dev", "reviewer"}
    dev = r["by_role"]["dev"]
    assert dev["input"] == 400 and dev["output"] == 100  # 100+300 / 40+60
    assert dev["measured_turns"] == 1 and dev["estimated_turns"] == 1
    rev = r["by_role"]["reviewer"]
    assert rev["input"] == 50 and rev["output"] == 10 and rev["partial_turns"] == 1
    # by_route split matches.
    assert r["by_route"]["rA"]["input"] == 400
    assert r["by_route"]["rB"]["input"] == 50


def test_estimated_portion_is_effective_minus_measured_not_raw_sum() -> None:
    # A measured turn also carries a raw estimated_input (for cli_overhead), but that
    # estimate must NOT inflate the headline's estimated portion — the portion is
    # effective - measured = 0 for a fully-measured turn.
    turns = [{"member_id": "m", "role": "dev", "route_id": "r",
              "usage": {"measured": True, "provenance": "measured",
                        "input_tokens": 100, "output_tokens": 40,
                        "measured_input": 100, "measured_output": 40,
                        "estimated_input": 999, "estimated_output": 999}}]
    r = rollup_turns(turns)
    assert r["total"]["input"] == 100 and r["total"]["output"] == 40
    assert r["total"]["measured_input"] == 100 and r["total"]["measured_output"] == 40
    # NOT 999 — the raw estimate is ignored; the headline is fully measured.
    assert r["total"]["estimated_input"] == 0 and r["total"]["estimated_output"] == 0
    assert r["total"]["coverage"]["measured_pct"] == 100


def test_all_unreported_gives_zero_coverage_no_crash() -> None:
    # Divide-by-zero guard: turns that contribute 0 headline tokens.
    turns = [
        {"member_id": "m", "role": "dev", "route_id": "r"},  # no usage block
        {"member_id": "m", "role": "dev", "route_id": "r",
         "usage": {"measured": True}},  # bare measured, no numbers
    ]
    r = rollup_turns(turns)
    assert r["total"]["input"] == 0 and r["total"]["output"] == 0
    assert r["total"]["turns"] == 2 and r["total"]["unreported_turns"] == 2
    assert r["total"]["coverage"] == {"measured_pct": 0, "estimated_pct": 0}


def test_dark_turn_effective_lands_in_headline_and_counts_estimated() -> None:
    # The motivating fix: a dark DEV turn's estimated spend must be IN the headline,
    # not dropped as a silent zero.
    turns = [{"member_id": "m-dev", "role": "dev", "route_id": "r",
              **_estimated(5000, 1200)}]
    r = rollup_turns(turns)
    assert r["total"]["input"] == 5000 and r["total"]["output"] == 1200
    assert r["total"]["estimated_input"] == 5000 and r["total"]["estimated_output"] == 1200
    assert r["total"]["measured_input"] == 0 and r["total"]["measured_output"] == 0
    assert r["total"]["estimated_turns"] == 1
    assert r["total"]["coverage"] == {"measured_pct": 0, "estimated_pct": 100}


def test_legacy_measured_block_without_provenance_still_counts() -> None:
    # Old F143 blocks lack a provenance field; inference keeps them measured.
    turns = [{"member_id": "m", "role": "dev", "route_id": "r",
              "usage": {"measured": True, "input_tokens": 10, "output_tokens": 5}}]
    r = rollup_turns(turns)
    assert r["total"]["input"] == 10 and r["total"]["output"] == 5
    assert r["total"]["measured_turns"] == 1
    # Legacy blocks predate byte-estimation: input_tokens/output_tokens WERE the
    # provider-reported counts (measured=True), so the whole headline is measured.
    # Attributing it as measured avoids inverting real provider spend into 100%
    # "estimated" on any upgraded project.
    assert r["total"]["coverage"]["measured_pct"] == 100
    assert r["total"]["measured_input"] == 10 and r["total"]["measured_output"] == 5


def test_routeless_and_bad_values_do_not_crash_or_fabricate() -> None:
    turns = [
        {"member_id": "m", **_measured(5, 2)},
        {"member_id": "m", "usage": {"measured": True, "provenance": "measured",
                                     "input_tokens": -4, "output_tokens": None,
                                     "measured_input": -4}},
        "not-a-dict",
        {"member_id": "m", "usage": {"measured": True, "provenance": "measured",
                                     "input_tokens": True, "output_tokens": 3,
                                     "measured_output": 3}},
    ]
    r = rollup_turns(turns)
    # routeless turns bucket under "unknown"; negative/None/bool inputs are dropped.
    assert "unknown" in r["by_route"]
    assert r["total"]["turns"] == 3  # the "not-a-dict" entry is skipped
    assert r["total"]["input"] == 5  # only the first turn's input is usable
    assert r["total"]["output"] == 5  # 2 + 3 (the bool-input turn still has output 3)
