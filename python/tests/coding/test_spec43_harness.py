"""Unit tests for the SPEC-43 verdict-usefulness harness.

NO NETWORK. Every model call is stubbed. The harness under test lives outside the
package (``docs/coding/model-eval/spec43_verdict_usefulness.py``, next to the F001
re-test), so it is loaded by path.

What is covered, per SPEC-43's definition of done:
  * the §3 decision rule picks the right §4 branch on synthetic inputs, including
    the "below threshold", "both at ceiling" and "both poor" cases;
  * the §2.3 shuffle is reproducible under a fixed seed AND genuinely decorrelates
    the arm from row order (and BlindRow cannot carry the arm at all);
  * the §6 corpus-dirty gate refuses to run;
  * OTHER-TRUE-POSITIVE is excluded from the numerator AND the denominator;
  * shallow and deep detection are reported separately.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys

import pytest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_HARNESS_PATH = os.path.join(
    _REPO_ROOT, "docs", "coding", "model-eval", "spec43_verdict_usefulness.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("spec43_harness", _HARNESS_PATH)
    assert spec and spec.loader, _HARNESS_PATH
    module = importlib.util.module_from_spec(spec)
    sys.modules["spec43_harness"] = module
    spec.loader.exec_module(module)
    return module


h = _load_harness()


# --------------------------------------------------------------------------- #
# Synthetic corpus
# --------------------------------------------------------------------------- #

CLASSES = ("off-by-one", "unhandled-error-path", "resource-leak", "null-guard",
           "race-ordering", "swallowed-exception", "wrong-comparison-operator",
           "missing-input-validation")


def make_items(classes=CLASSES):
    """8 classes x 2 depths seeded, each with its clean minimal-pair twin."""
    items = []
    for n, cls in enumerate(classes, start=1):
        for depth in ("shallow", "deep"):
            pair_id = f"{n:03d}{depth[0]}"
            base = f"{pair_id}-{cls}-{depth}"
            items.append(h.CorpusItem(
                id=base, kind="seeded", pair_id=pair_id, defect_class=cls,
                depth=depth, file=f"svc/{cls}.py", diff="@@ diff @@",
                ground_truth={"mechanism": f"{cls} mechanism",
                              "accepted_finding_forms": [f"{cls} form"],
                              "expected_verdict": "reject"}))
            items.append(h.CorpusItem(
                id=f"{base}-clean", kind="clean", pair_id=pair_id,
                defect_class=cls, depth=depth, file=f"svc/{cls}.py",
                diff="@@ diff @@",
                ground_truth={"expected_verdict": "approve", "note": "repaired"}))
    return items


def seeded(items, depth=None):
    return [i for i in items
            if i.seeded and (depth is None or i.depth == depth)]


def outcomes_for(items, *, deep_catches, shallow_catches=8, otp_ids=()):
    """Item -> outcome, with exactly ``deep_catches`` deep CATCHes."""
    out = {}
    for depth, wanted in (("deep", deep_catches), ("shallow", shallow_catches)):
        for n, item in enumerate(seeded(items, depth)):
            if item.id in otp_ids:
                out[item.id] = h.OTHER_TRUE_POSITIVE
            else:
                out[item.id] = h.CATCH if n < wanted else h.MISS
    return out


def approvals_for(items, *, clean_rejections=0):
    """Item -> approved. Seeded items are rejected; ``clean_rejections`` clean
    items are falsely rejected."""
    out = {}
    for item in items:
        if item.seeded:
            out[item.id] = False
        else:
            out[item.id] = True
    clean = [i for i in items if not i.seeded]
    for item in clean[:clean_rejections]:
        out[item.id] = False
    return out


def branch_for(items, per_item_outcome, per_item_approved):
    metrics = h.compute_metrics(items, per_item_outcome, per_item_approved, {})
    return h.select_outcome_branch(items, per_item_outcome, per_item_approved,
                                   metrics)


# --------------------------------------------------------------------------- #
# §3 arithmetic
# --------------------------------------------------------------------------- #

def test_exact_binomial_matches_hand_computation():
    # P(X <= 0 | n=5, p=.5) = 1/32; P(X <= 1 | n=5) = 6/32.
    assert h.binom_cdf_le(0, 5) == pytest.approx(1 / 32)
    assert h.binom_cdf_le(1, 5) == pytest.approx(6 / 32)
    assert h.binom_cdf_le(5, 5) == pytest.approx(1.0)
    # No discordant pairs at all must not fabricate significance.
    assert h.binom_cdf_le(0, 0) == 1.0


def test_paired_comparison_needs_both_size_and_significance():
    # 5 discordant pairs all one way: p = 1/32 = 0.031, diff 5 -> material.
    better = {f"i{n}": True for n in range(8)}
    worse = {f"i{n}": n >= 5 for n in range(8)}
    res = h.paired_comparison(better, worse)
    assert res["item_diff"] == 5
    assert res["discordant_better_only"] == 5
    assert res["discordant_worse_only"] == 0
    assert res["p_one_sided"] == pytest.approx(1 / 32, rel=1e-3)
    assert res["materially_below"] is True

    # 2 discordant pairs one way: p = 0.25 AND diff 2 -> fails BOTH legs.
    worse2 = {f"i{n}": n >= 2 for n in range(8)}
    res2 = h.paired_comparison(better, worse2)
    assert res2["item_diff"] == 2
    assert res2["materially_below"] is False

    # Size WITHOUT significance: diff 3, but only 3 discordant pairs, so
    # p = P(X <= 0 | n=3) = 0.125 > 0.10. Both legs are required.
    b3 = {f"i{n}": n < 5 for n in range(8)}
    w3 = {f"i{n}": 3 <= n < 5 for n in range(8)}
    res3 = h.paired_comparison(b3, w3)
    assert (res3["n_better"], res3["n_worse"]) == (5, 2)
    assert res3["item_diff"] == 3
    assert (res3["discordant_better_only"], res3["discordant_worse_only"]) == (3, 0)
    assert res3["p_one_sided"] == pytest.approx(0.125)
    assert res3["materially_below"] is False


# --------------------------------------------------------------------------- #
# §4 branch selection
# --------------------------------------------------------------------------- #

def test_branch_reasoning_suppression_when_both_arms_drop():
    items = make_items()
    outcome = {
        "T": outcomes_for(items, deep_catches=8),
        "U": outcomes_for(items, deep_catches=3),
        "S": outcomes_for(items, deep_catches=2),
    }
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "reasoning_suppression_costs_quality"
    assert res["in_spec_section_4"] is True


def test_branch_format_constraint_when_only_S_drops_below_U():
    items = make_items()
    outcome = {
        "T": outcomes_for(items, deep_catches=8),
        "U": outcomes_for(items, deep_catches=8),
        "S": outcomes_for(items, deep_catches=3),
    }
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "format_constraint_not_thinking"
    assert res["in_spec_section_4"] is True


def test_branch_below_threshold_is_not_upgraded_to_no_difference():
    """§4: a sub-threshold difference is 'no DETECTABLE difference at this
    power', explicitly not 'no difference' — and must not fall through to the
    'think:false is free' branch."""
    items = make_items()
    outcome = {
        "T": outcomes_for(items, deep_catches=6),
        "U": outcomes_for(items, deep_catches=5),
        "S": outcomes_for(items, deep_catches=4),   # diff 2 -> below threshold
    }
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "no_detectable_difference_at_this_power"
    assert res["in_spec_section_4"] is True
    assert res["basis"]["comparisons"]["deep_detection_S_below_T"][
        "materially_below"] is False


def test_branch_think_false_is_free_only_on_an_exactly_zero_gap():
    items = make_items()
    outcome = {arm: outcomes_for(items, deep_catches=6) for arm in ("T", "U", "S")}
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "think_false_is_free"


def test_branch_both_at_ceiling_beats_every_comparison():
    """>0.9 deep detection on every arm: the corpus is too easy and §4 says so
    'before concluding anything', so it must outrank the arm comparisons."""
    items = make_items()
    outcome = {arm: outcomes_for(items, deep_catches=8) for arm in ("T", "U", "S")}
    approved = {arm: approvals_for(items, clean_rejections=9) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "corpus_at_ceiling_rebuild_deeper"
    assert res["basis"]["deep_detection_rate"] == {"T": 1.0, "U": 1.0, "S": 1.0}


def test_branch_both_poor_defers_to_the_reference_pass():
    items = make_items()
    outcome = {
        "T": outcomes_for(items, deep_catches=3),
        "U": outcomes_for(items, deep_catches=3),
        "S": outcomes_for(items, deep_catches=2),
    }
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "corpus_at_floor_compare_reference"


def test_branch_false_rejection_regression_when_detection_ties():
    items = make_items()
    outcome = {arm: outcomes_for(items, deep_catches=6) for arm in ("T", "U", "S")}
    approved = {
        "T": approvals_for(items),
        "U": approvals_for(items),
        "S": approvals_for(items, clean_rejections=5),
    }
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "false_rejection_regression"
    assert res["basis"]["comparisons"]["false_rejection_S_worse_than_T"][
        "materially_below"] is True


def test_branch_per_class_collapse_governs_over_aggregate_parity():
    """A reviewer blind to one defect class is unfit regardless of its mean."""
    items = make_items()
    base = outcomes_for(items, deep_catches=6, shallow_catches=6)
    s = dict(base)
    # Blind S to one class entirely (both depths), and give it two catches back
    # elsewhere so the deep aggregate stays at parity with T.
    for item in items:
        if item.seeded and item.defect_class == "race-ordering":
            s[item.id] = h.MISS
    for item in seeded(items, "deep"):
        if s[item.id] == h.MISS and item.defect_class != "race-ordering":
            s[item.id] = h.CATCH
            if sum(1 for i in seeded(items, "deep") if s[i.id] == h.CATCH) >= 6:
                break
    outcome = {"T": base, "U": base, "S": s}
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "per_class_collapse_governs"
    assert any(hit["defect_class"] == "race-ordering"
               for hit in res["basis"]["per_class_collapse"]["classes"])


def test_unclassified_regression_is_flagged_as_outside_spec_section_4():
    """S materially below T while U ties T and S ties U is a pattern §4 never
    pre-stated; it must not be silently mapped onto a pre-stated branch."""
    items = make_items()
    t = outcomes_for(items, deep_catches=8)
    u = outcomes_for(items, deep_catches=8)
    # S misses 4 of T's deep catches but shares no discordant pair with U in the
    # direction that would make S<U significant... construct it directly:
    s = dict(t)
    deep_ids = [i.id for i in seeded(items, "deep")]
    for item_id in deep_ids[:4]:
        s[item_id] = h.MISS
    # U also misses 3 of those, so S-vs-U discordance is only 1 -> not material.
    for item_id in deep_ids[:3]:
        u[item_id] = h.MISS
    outcome = {"T": t, "U": u, "S": s}
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}
    res = branch_for(items, outcome, approved)
    assert res["outcome_branch"] == "unclassified_arm_regression"
    assert res["in_spec_section_4"] is False


def test_rescore_trigger_is_symmetric():
    """§3: ANY difference beyond the threshold, in either direction, triggers the
    blind re-score. Being sceptical only when the answer favours shipping would
    launder a prior."""
    items = make_items()
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S")}

    tie = {arm: outcomes_for(items, deep_catches=6) for arm in ("T", "U", "S")}
    assert h.rescore_triggered(branch_for(items, tie, approved)["basis"]) is False

    # S far BELOW T.
    down = dict(tie, S=outcomes_for(items, deep_catches=1))
    assert h.rescore_triggered(branch_for(items, down, approved)["basis"]) is True

    # S far ABOVE T — the direction that favours shipping. Still triggers.
    up = {"T": outcomes_for(items, deep_catches=1),
          "U": outcomes_for(items, deep_catches=1),
          "S": outcomes_for(items, deep_catches=6)}
    assert h.rescore_triggered(branch_for(items, up, approved)["basis"]) is True


def test_reference_arm_never_enters_the_decision_arithmetic():
    items = make_items()
    outcome = {arm: outcomes_for(items, deep_catches=6) for arm in ("T", "U", "S")}
    outcome["REF"] = outcomes_for(items, deep_catches=8)
    approved = {arm: approvals_for(items) for arm in ("T", "U", "S", "REF")}
    metrics = h.compute_metrics(
        items, outcome, approved, {},
        arms=(*h.ARMS, h.Arm("REF", think=True, format_json=False, model="gemma3:27b")))
    res = h.select_outcome_branch(items, outcome, approved, metrics)
    assert "REF" not in res["basis"]["deep_detection_rate"]
    assert res["outcome_branch"] == "think_false_is_free"
    # ...but it IS reported.
    assert metrics["REF"]["detection"]["deep"]["rate"] == 1.0


# --------------------------------------------------------------------------- #
# §2.3 blind shuffle
# --------------------------------------------------------------------------- #

def blind_input(n_items=16, arms=("T", "U", "S"), trials=3):
    return [
        {"item_id": f"item-{i:02d}", "arm": arm, "trial": t,
         "claim": f"claim {i} {arm} {t}", "mechanism": f"mech {i}",
         "accepted_finding_forms": [f"form {i}"], "depth": "deep",
         "defect_class": "off-by-one", "pair_id": f"{i:03d}"}
        for i in range(n_items) for arm in arms for t in range(trials)
    ]


def test_blind_row_cannot_carry_the_arm():
    rows, keymap = h.build_blind_rows(blind_input(), seed=7)
    row = rows[0]
    assert not hasattr(row, "arm")
    assert not hasattr(row, "item_id")
    assert "arm" not in json.dumps(row.__dict__)
    # ...and the arm is only reachable through the out-of-band keymap.
    assert keymap[row.row_uid]["arm"] in ("T", "U", "S")


def test_row_uid_does_not_leak_the_arm():
    """A readable uid like 'item-03-S-1' would re-attach the label the shuffle
    exists to detach."""
    rows, _ = h.build_blind_rows(blind_input(), seed=7)
    for row in rows:
        assert "-" not in row.row_uid
        assert row.row_uid.isalnum()
        for arm in ("|T|", "|U|", "|S|"):
            assert arm not in row.row_uid


def test_shuffle_is_reproducible_under_a_fixed_seed():
    records = blind_input()
    a, _ = h.build_blind_rows(records, seed=43)
    b, _ = h.build_blind_rows(records, seed=43)
    c, _ = h.build_blind_rows(records, seed=44)
    assert [r.row_uid for r in a] == [r.row_uid for r in b]
    assert [r.row_uid for r in a] != [r.row_uid for r in c]
    assert sorted(r.row_uid for r in a) == sorted(r.row_uid for r in c)


def test_shuffle_decorrelates_arm_from_row_order():
    """The input arrives in per-item arm blocks (T,T,T,U,U,U,S,S,S...). After the
    shuffle the arm must not be predictable from position."""
    records = blind_input()
    rows, keymap = h.build_blind_rows(records, seed=43)
    order = [keymap[r.row_uid]["arm"] for r in rows]
    assert len(order) == len(records)
    assert order != [rec["arm"] for rec in records]

    # The block structure is gone: in the input EVERY adjacent pair inside an
    # item's block shares an arm (2/3 of all pairs). After the shuffle the
    # neighbour-agreement rate must fall to chance (~1/3).
    def neighbour_agreement(seq):
        return sum(1 for a, b in zip(seq, seq[1:]) if a == b) / (len(seq) - 1)

    assert neighbour_agreement([rec["arm"] for rec in records]) > 0.6
    assert neighbour_agreement(order) == pytest.approx(1 / 3, abs=0.12)

    # Mean position per arm should sit near the middle for all three.
    n = len(order)
    for arm in ("T", "U", "S"):
        positions = [i for i, a in enumerate(order) if a == arm]
        mean = sum(positions) / len(positions)
        assert abs(mean - (n - 1) / 2) < n * 0.15, (arm, mean)


def test_join_after_scoring_restores_the_labels_exactly():
    records = blind_input(n_items=4)
    rows, keymap = h.build_blind_rows(records, seed=43)
    scored = {r.row_uid: h.CATCH for r in rows}
    collapsed = h.join_and_collapse(scored, keymap)
    assert set(collapsed) == {"T", "U", "S"}
    for arm in ("T", "U", "S"):
        assert len(collapsed[arm]["outcomes"]) == 4
        assert set(collapsed[arm]["outcomes"].values()) == {h.CATCH}


# --------------------------------------------------------------------------- #
# §6 corpus integrity gate
# --------------------------------------------------------------------------- #

def fake_git(*, tree_sha="deadbeef", porcelain=""):
    def _git(args, cwd):
        if args[0] == "rev-parse":
            assert args[1].startswith("HEAD:"), args
            return tree_sha
        if args[0] == "status":
            assert "--porcelain" in args and h.CORPUS_REL in args, args
            return porcelain
        raise AssertionError(f"unexpected git call {args}")
    return _git


def test_corpus_integrity_records_the_tree_sha():
    info = h.corpus_integrity(git=fake_git(tree_sha="abc123"))
    assert info["tree_sha"] == "abc123"
    assert info["clean"] is True
    assert info["path"] == h.CORPUS_REL


def test_dirty_corpus_gate_refuses_to_run():
    info = h.corpus_integrity(git=fake_git(
        porcelain=" M docs/coding/model-eval/verdict_corpus/items/001/ground_truth.json\n"))
    assert info["clean"] is False
    with pytest.raises(h.CorpusDirtyError) as exc:
        h.require_clean_corpus(info)
    assert "ground_truth.json" in str(exc.value)


def test_untracked_corpus_file_is_also_dirty():
    info = h.corpus_integrity(git=fake_git(
        porcelain="?? docs/coding/model-eval/verdict_corpus/items/033/diff.patch\n"))
    assert info["clean"] is False
    with pytest.raises(h.CorpusDirtyError):
        h.require_clean_corpus(info)


def test_run_refuses_before_making_any_model_call():
    calls = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the gate let a model call through")

    with pytest.raises(h.CorpusDirtyError):
        h.run(h.RunConfig(), call=boom,
              git=fake_git(porcelain=" M docs/coding/model-eval/verdict_corpus/x\n"))
    assert calls == []


def test_override_is_the_only_way_past_the_gate_and_stays_visible():
    info = h.corpus_integrity(git=fake_git(porcelain=" M a\n"))
    h.require_clean_corpus(info, override=True)   # does not raise
    assert info["clean"] is False                  # ...and the JSON still says so


def test_the_real_corpus_is_clean_and_its_sha_is_recorded():
    """Not a stub: the committed corpus must actually pass its own gate."""
    info = h.corpus_integrity()
    assert info["clean"] is True, info["dirty_entries"]
    assert len(info["tree_sha"]) == 40


def test_other_true_positive_hints_still_match_removals_md():
    h.assert_otp_hints_match_removals()
    assert set(h.OTHER_TRUE_POSITIVE_HINTS) == {"003", "012"}


# --------------------------------------------------------------------------- #
# OTHER-TRUE-POSITIVE exclusion + shallow/deep separation
# --------------------------------------------------------------------------- #

def test_other_true_positive_leaves_both_numerator_and_denominator():
    items = make_items()
    deep_items = seeded(items, "deep")
    outcome = outcomes_for(items, deep_catches=4)          # 4 catch, 4 miss deep
    plain = h.compute_metrics(items, {"T": outcome}, {"T": approvals_for(items)}, {},
                              arms=(h.ARMS[0],))["T"]["detection"]["deep"]
    assert (plain["catches"], plain["n"], plain["rate"]) == (4, 8, 0.5)

    # Flip one MISS to OTHER-TRUE-POSITIVE: it must leave the denominator, so the
    # rate RISES to 4/7 rather than staying 4/8.
    a_miss = next(i.id for i in deep_items if outcome[i.id] == h.MISS)
    outcome[a_miss] = h.OTHER_TRUE_POSITIVE
    with_otp = h.compute_metrics(items, {"T": outcome}, {"T": approvals_for(items)}, {},
                                 arms=(h.ARMS[0],))["T"]["detection"]["deep"]
    assert with_otp["catches"] == 4
    assert with_otp["n"] == 7
    assert with_otp["excluded_other_true_positive"] == 1
    assert with_otp["rate"] == pytest.approx(4 / 7, abs=1e-4)

    # And flipping a CATCH to OTHER-TRUE-POSITIVE must drop it from BOTH.
    a_catch = next(i.id for i in deep_items if outcome[i.id] == h.CATCH)
    outcome[a_catch] = h.OTHER_TRUE_POSITIVE
    both = h.compute_metrics(items, {"T": outcome}, {"T": approvals_for(items)}, {},
                             arms=(h.ARMS[0],))["T"]["detection"]["deep"]
    assert (both["catches"], both["n"]) == (3, 6)


def test_other_true_positive_items_leave_the_paired_decision_test_too():
    """An OTP item must not silently become a MISS on one side of the McNemar
    table — that would re-import the bias the exclusion exists to remove."""
    items = make_items()
    t = outcomes_for(items, deep_catches=8)
    s = outcomes_for(items, deep_catches=8)
    otp_id = seeded(items, "deep")[0].id
    s[otp_id] = h.OTHER_TRUE_POSITIVE
    res = h.paired_comparison(h._deep_success(items, t), h._deep_success(items, s))
    assert res["n_items"] == 7          # the OTP item is gone from both sides
    assert res["item_diff"] == 0
    assert res["materially_below"] is False


def test_an_all_other_true_positive_item_is_excluded_entirely():
    outcome, rate = h.collapse_trials([h.OTHER_TRUE_POSITIVE] * 3)
    assert outcome == h.OTHER_TRUE_POSITIVE
    # A single surviving trial decides; OTP trials never vote.
    assert h.collapse_trials([h.OTHER_TRUE_POSITIVE, h.OTHER_TRUE_POSITIVE,
                              h.CATCH]) == (h.CATCH, 1.0)
    assert h.collapse_trials([h.CATCH, h.CATCH, h.MISS])[0] == h.CATCH
    assert h.collapse_trials([h.CATCH, h.MISS, h.MISS])[0] == h.MISS
    assert rate == 0.0


def test_shallow_and_deep_are_reported_separately_and_never_pooled():
    items = make_items()
    outcome = outcomes_for(items, deep_catches=2, shallow_catches=7)
    metrics = h.compute_metrics(items, {"T": outcome}, {"T": approvals_for(items)}, {},
                                arms=(h.ARMS[0],))["T"]["detection"]
    assert metrics["deep"]["rate"] == 0.25
    assert metrics["shallow"]["rate"] == 0.875
    assert metrics["aggregate"]["rate"] == pytest.approx(9 / 16)
    assert metrics["deep"]["rate"] != metrics["shallow"]["rate"]
    # Per class, per depth, both present.
    for cls in CLASSES:
        assert set(metrics["by_class"][cls]) == {"aggregate", "shallow", "deep"}
        assert metrics["by_class"][cls]["deep"]["n"] == 1
        assert metrics["by_class"][cls]["shallow"]["n"] == 1


def test_false_approval_and_false_rejection_use_the_right_subsets():
    items = make_items()
    approved = approvals_for(items, clean_rejections=3)
    # Two seeded items wrongly approved.
    for item in seeded(items)[:2]:
        approved[item.id] = True
    m = h.compute_metrics(items, {"T": outcomes_for(items, deep_catches=4)},
                          {"T": approved}, {}, arms=(h.ARMS[0],))["T"]
    assert m["false_approval"]["count"] == 2 and m["false_approval"]["n"] == 16
    assert m["false_rejection"]["count"] == 3 and m["false_rejection"]["n"] == 16


# --------------------------------------------------------------------------- #
# Claim extraction + scoring (both stubbed)
# --------------------------------------------------------------------------- #

def reply(text):
    return h.ModelReply(text=text, done_reason="stop", eval_count=7, wall_s=0.01)


def test_claim_is_capped_at_twenty_words_after_the_model_returns():
    """The cap is what removes the length bias by which a lenient semantic match
    is likelier in 1455 tokens than in 91."""
    long_claim = " ".join(f"word{n}" for n in range(200))
    verdict = h.Verdict(True, "direct", False,
                        [{"severity": "blocking", "path": "a.py",
                          "title": "t", "body": "b"}], "head", True)
    claim = h.extract_claim(verdict, call=lambda *a, **k: reply(long_claim))
    assert len(claim.split()) == h.CLAIM_WORD_CAP


def test_extraction_makes_no_model_call_for_approvals_or_unparseables():
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return reply("x")

    approved = h.Verdict(True, "direct", True, [], "head", True)
    assert h.extract_claim(approved, call=spy) == h.NO_CLAIM
    unparseable = h.Verdict(False, "unparseable", None, [], "", False)
    assert h.extract_claim(unparseable, call=spy) == h.NO_CLAIM
    no_findings = h.Verdict(True, "direct", False, [], "head", True)
    assert h.extract_claim(no_findings, call=spy) == h.NO_CLAIM
    assert calls == []


def test_extractor_sees_at_most_three_findings_ordered_by_severity():
    """Caps the credit a shotgun reviewer earns by burying a true claim in a
    list, without punishing a short verdict."""
    findings = [
        {"severity": "minor", "path": "a.py", "title": "nit", "body": "n"},
        {"severity": "blocking", "path": "b.py", "title": "real", "body": "r"},
        {"severity": "major", "path": "c.py", "title": "mid", "body": "m"},
        {"severity": "minor", "path": "d.py", "title": "nit2", "body": "n2"},
    ]
    shown = h.findings_for_extraction(findings)
    assert [f["title"] for f in shown] == ["real", "mid", "nit"]

    seen = {}

    def spy(model, prompt, **kwargs):
        seen["prompt"] = prompt
        return reply("b.py loses the guard")

    verdict = h.Verdict(True, "direct", False, findings, "head", True)
    h.extract_claim(verdict, call=spy)
    assert "nit2" not in seen["prompt"]
    assert "real" in seen["prompt"]


def test_scorer_sees_only_the_blind_fields():
    seen = {}

    def spy(model, prompt, **kwargs):
        seen["prompt"] = prompt
        return reply("CATCH")

    row = h.BlindRow(row_uid="abc", claim="the read is one byte short",
                     mechanism="off-by-one against an inclusive range",
                     accepted_finding_forms=("range end is inclusive",),
                     other_true_positive_hint="")
    outcome, failed = h.score_claim(row, call=spy)
    assert (outcome, failed) == (h.CATCH, False)
    prompt = seen["prompt"]
    assert "the read is one byte short" in prompt
    assert "off-by-one against an inclusive range" in prompt
    # Nothing about the configuration is reachable from the prompt.
    for leak in ("think", "format", "arm", "trial", "qwen", "gemma", "temperature"):
        assert not re.search(rf"\b{leak}\b", prompt, re.I), leak
    # The prompt is exactly the template rendered from the four blind fields, so
    # there is no other channel for an arm label to arrive through.
    assert prompt == h._SCORE_PROMPT.format(
        mechanism=row.mechanism,
        forms="\n".join(f"  - {f}" for f in row.accepted_finding_forms),
        otp_hint="", claim=row.claim)


def test_scorer_surfaces_the_removals_md_other_true_positive_instruction():
    seen = {}

    def spy(model, prompt, **kwargs):
        seen["prompt"] = prompt
        return reply("OTHER-TRUE-POSITIVE")

    row = h.BlindRow(row_uid="abc", claim="_snapshot is rebound without a lock",
                     mechanism="stale config on failure",
                     accepted_finding_forms=("the fetch error is unhandled",),
                     other_true_positive_hint=h.OTHER_TRUE_POSITIVE_HINTS["003"])
    outcome, _ = h.score_claim(row, call=spy)
    assert outcome == h.OTHER_TRUE_POSITIVE
    assert "OTHER-TRUE-POSITIVE, not CATCH" in seen["prompt"]


def test_empty_claim_is_a_miss_with_no_model_call():
    calls = []
    row = h.BlindRow("uid", "", "mech", ())
    assert h.score_claim(row, call=lambda *a, **k: calls.append(a)) == (h.MISS, False)
    assert calls == []


def test_outcome_parsing_prefers_the_longest_token_and_flags_junk():
    assert h.parse_outcome("OTHER-TRUE-POSITIVE") == (h.OTHER_TRUE_POSITIVE, False)
    assert h.parse_outcome("other true positive") == (h.OTHER_TRUE_POSITIVE, False)
    assert h.parse_outcome("  catch  ") == (h.CATCH, False)
    assert h.parse_outcome("MISS") == (h.MISS, False)
    # Unreadable: scored MISS, but FLAGGED so the count is reported.
    assert h.parse_outcome("I'm not sure") == (h.MISS, True)


# --------------------------------------------------------------------------- #
# Production envelope (§2.4) + end-to-end with every model call stubbed
# --------------------------------------------------------------------------- #

def test_prompt_is_the_production_coding_turn_v1_envelope():
    items = h.load_corpus()
    item = items[0]
    prompt = h.review_prompt(item)
    assert '"schema_version": "coding_turn.v1"' in prompt
    assert '"kind": "review_verdict"' in prompt
    assert '"reviewed_head"' in prompt and item.head in prompt
    assert '"approved"' in prompt and '"findings"' in prompt
    assert "You are a reviewer for task id" in prompt
    assert item.diff in prompt


def test_twins_of_a_pair_differ_only_in_the_diff():
    """Anything else that differed would be a leak telling the reviewer which
    twin is the seeded one."""
    items = {i.id: i for i in h.load_corpus()}
    seeded_item = items["001-off-by-one-shallow"]
    clean_item = items["001-off-by-one-shallow-clean"]
    a = h.review_prompt(seeded_item).replace(seeded_item.diff, "<DIFF>")
    b = h.review_prompt(clean_item).replace(clean_item.diff, "<DIFF>")
    # The synthetic head is per-item by construction; normalise it out.
    a = a.replace(seeded_item.head, "<HEAD>")
    b = b.replace(clean_item.head, "<HEAD>")
    assert a == b


def envelope(head, approved, findings=()):
    return json.dumps({
        "schema_version": "coding_turn.v1", "role": "reviewer",
        "task_id": "spec43-review",
        "intent": {"kind": "review_verdict", "reviewed_head": head,
                   "approved": approved, "findings": list(findings)},
    })


def test_parse_verdict_reads_the_production_envelope_and_its_aliases():
    v = h.parse_verdict(envelope("abc123", False, [
        {"severity": "blocking", "title": "t", "body": "b", "path": "x.py"}]), "abc123")
    assert (v.parsed, v.approved, v.head_echo_ok) == (True, False, True)
    assert v.findings[0]["path"] == "x.py"

    # Wrong echo is recorded, not fatal.
    assert h.parse_verdict(envelope("nope", True), "abc123").head_echo_ok is False

    # Council-tolerated shapes: nested location, `description`, string findings.
    v2 = h.parse_verdict(envelope("abc123", False, [
        {"severity": "high", "location": {"path": "y.py", "line": 12},
         "description": "the guard is gone"}, "a bare string finding"]), "abc123")
    assert v2.findings[0]["path"] == "y.py"
    assert v2.findings[0]["body"] == "the guard is gone"
    assert v2.findings[1]["title"] == "a bare string finding"

    # Prose-wrapped JSON (the arm-T failure mode) still reads.
    assert h.parse_verdict("Sure!\n```json\n" + envelope("abc123", True) + "\n```",
                           "abc123").parsed is True
    # Genuinely unparseable is not silently an approval.
    bad = h.parse_verdict("I think this looks fine to me.", "abc123")
    assert (bad.parsed, bad.approved) == (False, None)


def test_end_to_end_run_with_every_model_call_stubbed():
    """No network anywhere: one stub answers the reviewer, the extractor and the
    scorer. Arm S is made blind to the deep subset; the harness must reach a §4
    branch on its own."""
    seen_models = set()

    def stub(model, prompt, **kwargs):
        seen_models.add(model)
        if "REVIEWER CLAIM" in prompt:                       # scorer
            return reply(h.MISS if "NOTHING" in prompt else h.CATCH)
        if "normalising a code-review verdict" in prompt:    # extractor
            return reply("the planted defect, in under twenty words")
        # reviewer: reject everything, but emit an empty claim (-> MISS) for the
        # deep items when the arm is the format-constrained one.
        head = prompt.split("The PR head you are reviewing is '")[1].split("'")[0]
        blind = kwargs.get("format_json") and "deep" in prompt
        body = "NOTHING" if blind else "the planted defect"
        return reply(envelope(head, False, [
            {"severity": "blocking", "path": "svc.py", "title": body, "body": body}]))

    out = h.run(h.RunConfig(trials=1, limit=4), call=stub, git=fake_git())
    assert out["corpus_integrity"]["tree_sha"] == "deadbeef"
    assert out["config"]["shuffle_seed"] == h.DEFAULT_SEED
    assert out["config"]["temperature"] == 0.3
    assert out["config"]["num_ctx"] == 8192 and out["config"]["num_predict"] == 8192
    assert [a["name"] for a in out["config"]["arms"]] == ["T", "U", "S"]
    assert seen_models == {h.MODEL_UNDER_TEST, h.SCORER_MODEL}
    assert out["counts"]["items"] == 8          # 4 pairs
    assert out["counts"]["review_calls"] == 8 * 3
    assert out["outcome_branch"] in {
        "corpus_at_ceiling_rebuild_deeper", "corpus_at_floor_compare_reference",
        "reasoning_suppression_costs_quality", "format_constraint_not_thinking",
        "per_class_collapse_governs", "false_rejection_regression",
        "no_detectable_difference_at_this_power", "think_false_is_free",
        "unclassified_arm_regression"}
    # Detection is reported separately per depth, per class, for every arm.
    for arm in ("T", "U", "S"):
        det = out["metrics"][arm]["detection"]
        assert {"shallow", "deep", "aggregate", "by_class"} <= set(det)
        assert out["metrics"][arm]["cost"]["calls"] == 8
    # The blind pipeline is on record: seed, row order, keymap, scored rows.
    assert len(out["blind_row_order"]) == out["counts"]["blind_rows"]
    assert set(out["row_keymap"]) == set(out["scored_rows"])
    assert out["rescore_sample_row_uids"]
    assert json.dumps(out)                      # the whole thing serialises


def test_outcome_branch_is_emitted_before_any_prose_exists():
    """§6: the machine-selected branch is on record either way. It must be a
    top-level key of the archived JSON, with its arithmetic beside it."""
    def stub(model, prompt, **kwargs):
        if "REVIEWER CLAIM" in prompt:
            return reply(h.CATCH)
        if "normalising a code-review verdict" in prompt:
            return reply("a claim")
        head = prompt.split("The PR head you are reviewing is '")[1].split("'")[0]
        return reply(envelope(head, False, [
            {"severity": "blocking", "path": "s.py", "title": "x", "body": "x"}]))

    out = h.run(h.RunConfig(trials=1, limit=2), call=stub, git=fake_git())
    assert isinstance(out["outcome_branch"], str)
    assert isinstance(out["outcome_branch_in_spec_section_4"], bool)
    assert "comparisons" in out["decision_basis"]
    assert "deep_detection_S_below_T" in out["decision_basis"]["comparisons"]
    assert isinstance(out["rescore_triggered"], bool)


# --------------------------------------------------------------------------- #
# kappa gate helpers (§2.3)
# --------------------------------------------------------------------------- #

def test_cohens_kappa_and_the_void_threshold():
    labels = [h.CATCH, h.MISS, h.CATCH, h.MISS, h.OTHER_TRUE_POSITIVE, h.CATCH]
    assert h.cohens_kappa(labels, labels) == pytest.approx(1.0)
    disagree = [h.MISS, h.CATCH, h.MISS, h.CATCH, h.CATCH, h.MISS]
    assert h.cohens_kappa(labels, disagree) < h.KAPPA_VOID_THRESHOLD
    with pytest.raises(ValueError):
        h.cohens_kappa([], [])


def test_rescore_sample_is_a_reproducible_blind_quarter():
    uids = [f"uid{n:03d}" for n in range(96)]
    a = h.select_rescore_sample(uids, seed=43)
    b = h.select_rescore_sample(uids, seed=43)
    c = h.select_rescore_sample(uids, seed=44)
    assert a == b and a != c
    assert len(a) == 24
    assert set(a) <= set(uids)
