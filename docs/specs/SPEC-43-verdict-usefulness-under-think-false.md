# SPEC-43 — Verdict usefulness under `think:false`

**Status:** proposed · **Owner:** wiggins-j
**Relates to:** [SPEC-41](SPEC-41-local-model-integration.md) (shipped `think:false` +
`format:"json"` on structured local turns) · [SPEC-42](SPEC-42-coding-turn-output-budget.md)
(named this as the highest-value follow-up and gated it on the budget fix) ·
[F001](F001-judge-and-grounding-loop.md) (the judge re-test that measured shape only)

---

## 1. The question, and why the obvious metric is unsound

SPEC-41 ships `think:false` on every structured LOCAL turn. Measured effect on the
reviewer-verdict shape: schema compliance 2/8 → 8/8, generated tokens 1455 → 91.

**Everything measured so far scores SHAPE, not CONTENT.** The F001 re-test asked "is
this well-formed JSON matching the verdict schema?" and got 8/8. It never asked
whether the verdict was *right*. We have suppressed a reasoning model's reasoning by
a factor of ~16 in generated tokens and verified only that what comes back parses.

The council merges on reviewer verdicts. If a reviewer that no longer reasons emits
well-formed JSON that says nothing actionable, every gate built on top inherits the
flaw, and nothing currently in the suite would detect it.

### 1.1 Merge-gate pass rate is a DEGENERATE oracle on its own

SPEC-42 §"Only after both" proposed scoring "a real council run on merge-gate pass
rate rather than isolated function correctness." Taken literally that is unsound, and
in exactly the way this project has been burned before (see the gravity-golf
inert-mechanic oracle): **a reviewer that approves everything scores a 100% pass
rate.** Pass rate rises monotonically as the reviewer becomes more useless. Optimising
it selects for the failure mode this spec exists to detect.

The same applies in reverse: a reviewer that rejects everything has a perfect
defect-detection rate and is equally worthless.

So the oracle must be **differential and two-sided**: seeded defects that a useful
reviewer must CATCH, and clean code a useful reviewer must PASS. Neither number means
anything alone.

## 2. Design

Two arms, identical in every other respect, run against the same corpus on senditai:

| arm | configuration |
|---|---|
| **T** (thinking) | budget 8192, thinking ON, no `format` |
| **S** (shipped) | budget 8192, `think:false`, `format:"json"` — what main does today |

Arm T is the control, not the baseline-to-beat: it is the configuration whose verdict
quality we have historically trusted, now that SPEC-42 has removed the truncation that
made it unmeasurable.

### 2.1 Corpus

`docs/coding/model-eval/verdict_corpus/` — N ≥ 12 review items, each a small diff plus
the ground truth about it. Two classes, balanced:

* **SEEDED** — contains exactly one planted defect of a known class, with the expected
  finding recorded. Classes to cover, one item minimum each:
  off-by-one / boundary; unhandled error path; resource leak (unclosed handle);
  incorrect null/None guard; race or ordering assumption; silently swallowed
  exception; wrong comparison operator; missing input validation.
* **CLEAN** — correct code, some of it deliberately *unusual-looking* (an early
  return, a deliberate bare `except` with a comment, a hand-rolled loop where a
  comprehension would do). These catch a reviewer that pattern-matches on style
  rather than reading for defects.

Ground truth is authored by hand and reviewed BEFORE any arm runs. An item whose
correct verdict is arguable is removed, not adjudicated after the fact.

### 2.2 Metrics

Primary — usefulness:
* **detection rate** = seeded defects correctly flagged `request_changes` **with a
  finding that names the actual defect**. A `request_changes` with an unrelated
  finding counts as a MISS, not a catch; that distinction is the whole point.
* **false-approval rate** = seeded items approved.
* **false-rejection rate** = clean items rejected.

Secondary — cost, interpretable only alongside the above:
* merge-gate pass rate, mean generated tokens, wall-clock per verdict.

Scoring of "names the actual defect" is done by a human or by a STRONGER model than
either arm, blind to which arm produced the verdict. Self-scoring by a model under
test is not admissible.

### 2.3 Trials

Each item × each arm × 3 trials at temperature 0.3, so a single unlucky sample cannot
carry a conclusion. Report per-item as well as aggregate: a uniform small drop and a
catastrophic failure on one defect class are very different findings and the aggregate
hides the second.

## 3. Pre-stated outcomes

Written down before the run, per this project's convention:

* **S ≈ T on detection, S better on cost** → `think:false` is free. It ships as-is and
  this question is closed.
* **S materially below T on detection** (the risk this spec exists for) → the token
  saving was bought with verdict quality. `local_think_false` already exists as a knob
  with a disable value that reproduces prior behaviour; the response is to default it
  OFF for REVIEWER seats specifically, keeping it on where structure matters more than
  reasoning. Note this does NOT revert SPEC-42 — the budget fix is what made arm T
  viable at all.
* **S above T on detection** → surprising; treat as suspect. Most likely explanation is
  that arm T's verdicts are being mis-scored because their findings are buried in
  prose. Re-score before believing it.
* **Both arms poor in absolute terms** (detection < ~0.5) → the interesting result is
  not the arm comparison at all; it is that a 9B local reviewer is not fit for the
  reviewer seat, which is an F001-adjacent conclusion about seat assignment and would
  reopen the judge-model question on QUALITY grounds — the grounds the F001 re-test
  explicitly could not speak to.

## 4. Non-goals

* Not a benchmark of models against each other. One model, two configurations.
* Not a measure of PM or DEV turn quality. Reviewer seat only — that is where the
  merge gate reads.
* Not a replacement for the deterministic gates. A verdict-quality number does not
  license loosening F154/F156.

## 5. Definition of done

* Corpus committed with ground truth, authored before any run.
* Harness in `docs/coding/model-eval/`, runnable on senditai, raw JSON archived under
  `results/` like the F001 re-test.
* Both arms run, per-item and aggregate reported.
* The outcome recorded against the pre-stated readings in §3 — including, explicitly,
  if the result is the inconvenient one.
* SPEC-41's `local_think_false` documentation updated with the finding either way.
