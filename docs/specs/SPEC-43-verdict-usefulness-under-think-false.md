# SPEC-43 — Verdict usefulness under `think:false`

**Status:** proposed (rev. 2 — revised after an adversarial review that found the rev. 1
design could not detect the degradation it was written to find) · **Owner:** wiggins-j
**Relates to:** [SPEC-41](SPEC-41-local-model-integration.md) (shipped `think:false` +
`format:"json"`, and carries the default gate this spec must discharge) ·
[SPEC-42](SPEC-42-coding-turn-output-budget.md) (named this follow-up and gated it on
the budget fix) · [F001](F001-judge-and-grounding-loop.md) (the re-test that measured
shape only)

---

## 1. The question

SPEC-41 ships `think:false` and `format:"json"` on every structured LOCAL turn.

**State the measured effect honestly, because rev. 1 of this spec got it wrong.** Rev. 1
claimed "schema compliance 2/8 → 8/8, generated tokens 1455 → 91". That quotes the shape
gain from the *pre*-SPEC-42 arm and the token cost from the *post*-SPEC-42 arm — a
headline no single comparison supports. From the archived results
(`docs/coding/model-eval/results/f001-judge-retest-2026-08-06.json`, `qwen3.5:9b`):

| arm | schema_ok | direct_parse | mean tokens |
|---|---|---|---|
| A — 2048, think on (pre-SPEC-42) | 2/8 | 0/8 | 1678 |
| B — 8192, think on | **7/8** | 5/8 | **1455** |
| C — 8192, `think:false`, `format:"json"` | **8/8** | 8/8 | **91** |

The only comparison relevant to a decision *today* is **B → C**, because SPEC-42 has
landed and B is the current alternative. That comparison is **7/8 → 8/8** — one sample
at n=8, statistically nothing — and **1455 → 91 tokens**, a ~16× saving.

**So `think:false` bought speed, not shape.** The shape argument was real only against
the truncated pre-SPEC-42 baseline, and SPEC-42 removed that baseline. This matters
because it sets what a null result licenses: if the arms tie on usefulness, the case for
`think:false` rests *entirely* on cost; if the shipped arm is worse, the trade is not
close.

**Everything measured so far scores SHAPE, not CONTENT.** 8/8 means well-formed JSON
matching the verdict schema. Nothing has asked whether the verdict was *right*. We have
suppressed a reasoning model's reasoning ~16× in generated tokens and verified only that
what comes back parses. The council merges on reviewer verdicts, so if a reviewer that
no longer reasons emits well-formed JSON saying nothing actionable, every gate above it
inherits the flaw and nothing in the suite would notice.

### 1.1 Merge-gate pass rate is a DEGENERATE oracle

SPEC-42 proposed scoring "a real council run on merge-gate pass rate." Taken literally
that is unsound in this project's signature way: **a reviewer that approves everything
scores 100%.** Pass rate rises monotonically as the reviewer becomes useless. A reviewer
that rejects everything is the mirror failure.

So the oracle is **two-sided**: seeded defects that must be CAUGHT, and clean code that
must PASS.

### 1.2 …and two-sidedness alone is still not enough

Two-sidedness closes the *shotgun* reviewer (stock finding list on every item — 100%
detection, 100% false rejection). It does **not** close this one:

> **Salient-surface-anomaly matching.** Never reason about behaviour. Find the single
> most locally-odd token — `<=` beside a `len()`, an `open()` with no `with`, a bare
> `except`, a missing `is None` — and emit the canonical finding for it. Approve when
> nothing looks locally odd.

That strategy scores high on *both* metrics, because a hand-planted single defect **is**
the salient local anomaly, and style-odd clean code trips a different discriminator. It
is also exactly what a de-reasoned model retains. The failure mode we actually fear —
loss of **multi-step** reasoning — is the one an all-shallow corpus cannot probe.

Hence the shallow/deep stratification in §2.2, and §4 keying the decision on the **deep**
subset.

## 2. Design

### 2.1 Three arms, not two

Rev. 1 compared only `(think on, format off)` against `(think off, format on)` — two
variables moving at once, so a difference is unattributable. The gate at
`gateway_local.py:376-380` is **one-directional**: it blocks only `(think on, format on)`
— issue #84's harmful pairing. `(think off, format off)` is reachable through the
existing independent knobs (`local_think_false=True`, `local_structured_format=False`).

| arm | think | format | note |
|---|---|---|---|
| **T** | on | off | control; = F001 arm B |
| **U** | off | off | **the missing cell** — isolates thinking-suppression from constrained decoding |
| **S** | off | on | what main ships; = F001 arm C |
| — | on | on | unreachable by design (#84) |

Arm U is what separates "suppressing reasoning cost us quality" from "constrained
decoding cost us substance" — and #84's own caveat names the second as the live risk.
Cost of adding it: one extra pass, no new corpus, no new machinery.

### 2.2 Corpus

`docs/coding/model-eval/verdict_corpus/`. **N = 32**: 16 seeded + 16 clean.

Seeded items cover 8 defect classes × 2 depths:
off-by-one/boundary; unhandled error path; resource leak; incorrect null guard;
race or ordering assumption; swallowed exception; wrong comparison operator; missing
input validation.

* **shallow** — defect visible in one hunk.
* **deep** — defect requires composing two facts: a guard removed in A that B relies on;
  a lock released before the read that uses it; an error path wrong only given a retry
  policy shown elsewhere in the diff.

**Minimal pairs.** Every clean item is the identical file with the defect repaired — not
an unrelated clean file. Disjoint sets let a reviewer discriminate on incidental base-file
features rather than on the defect, and leave the false-rejection number paired with
nothing.

**Ground truth** is authored by hand before any arm runs, and carries per item:
`defect_class`, `depth`, `mechanism`, and **`accepted_finding_forms`** — the set of
phrasings that count as correct, written in advance.

**Removal rule, split in two** (rev. 1 conflated these, biasing the corpus toward easy
defects):
* *arguable ground truth* ("is this even a defect?") → **remove**.
* *arguable phrasing* ("a correct finding could be worded several ways") → **keep**, and
  enumerate the wordings in `accepted_finding_forms`. This is what retains races,
  ordering, and resource-lifetime defects, whose correct findings are legitimately
  location-vague.

Removed items and their reason are logged in the corpus dir. If removals skew deep, that
is itself a finding about corpus difficulty and must be visible.

### 2.3 Scoring — mechanically blind

Blindness cannot be asserted: arm T emits multi-hundred-token prose, arm S emits ~91
tokens of flat JSON. Any scorer identifies the arm from the first line. So it is
**constructed**, by committed code:

1. A mechanical extraction pass reduces each verdict to a **normalised claim** — defect
   location + mechanism, ≤20 words — before any comparison. This also removes most of
   the length bias by which a lenient semantic match is likelier in 1455 tokens than in 91.
2. Claims are shuffled across arms and items, one per row; the arm label is held
   out-of-band and joined only after every row is scored.
3. The scorer sees only `(normalised claim, ground-truth mechanism, accepted forms)`.

**Three outcomes, not two:**
* `CATCH` — names the planted defect (matching mechanism-and-consequence, not location).
* `OTHER-TRUE-POSITIVE` — a genuine defect not in ground truth. Excluded from numerator
  *and* denominator; the item is flagged for corpus repair. Without this, a reviewer that
  is *more* correct than the corpus is scored as a MISS.
* `MISS`.

**Scorer.** Not "a stronger model" — that is both unavailable (both arms are
`qwen3.5:9b`; `gemma3:27b` and `mistral-small3.1` are not demonstrably stronger at code
review) and the wrong criterion. The job is short-text semantic matching against supplied
ground truth, for which `gemma3:27b` is adequate. Plus **human double-scoring of a 25%
sample**, reporting Cohen's κ. **κ < 0.6 voids the run**; the rubric is revised and
scoring repeated. Without an agreement number, "names the actual defect" is precisely the
unfalsifiable judgement call this spec exists to avoid.

**Reference pass.** `gemma3:27b` also runs the corpus *as a reviewer*, one extra pass, to
calibrate difficulty (is the corpus at ceiling or floor?) and to give §4's absolute
readings a referent.

### 2.4 Trials and production fidelity

Each item × arm × 3 trials at temperature 0.3 (production default, `runner.py:8169`).
The 3 trials estimate within-item variance; **they do not multiply N** — the unit of
analysis is the item.

The harness uses the **production `coding_turn.v1` envelope** — nested `intent` with
`kind`, the `reviewed_head` echo, `approved`, `findings[{severity,title,body}]` — and
assembles prompts through `_review_pr_prompt_segments`, not hand-written toy prompts.
Constraining a nested envelope *with a required echoed hash* is materially harder than
the flat toy schema the F001 re-test used, so an arm-S result on the toy shape may not
transfer.

## 3. Power, and the decision rule — written before the run

**Minimum detectable effect.** With 16 seeded items in a paired comparison, roughly a
15–20pp difference in detection is distinguishable from noise. Rev. 1's N=12 (≈6 seeded)
could only detect a ≥30–40pp collapse, while §2.3 of that draft promised to surface "a
uniform small drop" — undetectable at that N. **This run is powered as a screen for a
material regression, not as a precision estimate.**

**Decision rule, as arithmetic:** *materially below* = a **deep-subset** detection
difference of ≥3 items with one-sided exact-binomial p ≤ 0.10 on discordant pairs.

**Stopping rule:** full corpus, no interim peeking.

**Re-score trigger, symmetric:** *any* arm difference beyond the threshold — in either
direction — triggers a blind re-score of a random 25% sample by the second scorer. Rev. 1
was sceptical only when the result favoured shipping, which pre-commits to credulity
exactly when the answer is "revert." That launders a prior.

## 4. Pre-stated outcomes

Keyed on the **deep** subset unless stated.

* **S ≈ U ≈ T on detection** → `think:false` is free; the cost saving is real and this
  question closes. Note this licenses cost *only*, per §1.
* **S < T and U < T** → suppressing the reasoning cost verdict quality. Remedy: default
  `local_think_false` OFF for REVIEWER seats, keeping it where structure matters more
  than reasoning. Does **not** revert SPEC-42 — the budget fix is what made T viable.
* **U ≈ T but S < U** → it is the `format` constraint, not the thinking suppression.
  Different remedy: drop `local_structured_format`, keep the token saving.
* **Detection ties, false-rejection differs** → the two-sided oracle's other axis. A
  material false-rejection rise is a real regression (the council stalls on clean code)
  and is treated as `S < T` for remedy purposes.
* **Difference below the §3 threshold** → report as "no detectable difference at this
  power," explicitly NOT as "no difference." Do not upgrade a screen to a proof.
* **Both arms at ceiling (>0.9 deep detection)** → corpus too easy; rebuild deeper before
  concluding anything.
* **Both arms poor (<0.5 deep detection)** → compare against the `gemma3:27b` reference
  pass before concluding anything about seat fitness. Without a referent, "poor" could
  equally mean the corpus is too hard.
* **Aggregate parity with a per-class collapse** → the per-class result governs. A
  reviewer blind to one defect class is not fit for the seat regardless of its mean.
* **Unparseable arm-T verdicts** (measured 1/8) → scored as MISS, and reported separately
  so the contamination is visible. Dropping them would hand T a survivorship bonus.

## 5. Non-goals

* Not a benchmark of models against each other. One model under test, three
  configurations; `gemma3:27b` appears only as a difficulty referent.
* Not a measure of PM or DEV turn quality. Reviewer seat only.
* Not a licence to loosen the deterministic gates (F154/F156).
* **Scope limit:** the corpus is isolated diffs. Production reviewers under SPEC-32 get a
  repo mount and are expected to open files. If `think:false` degrades gracefully on a
  300-token diff and badly on a 6k-token grounded review, this design measures the
  former. Stated here rather than left implied.

## 6. Definition of done

* Corpus + ground truth (with `accepted_finding_forms`, `depth`, removal log) committed
  in **its own PR, merged before the harness PR**. The harness records the corpus tree
  SHA into the results JSON and **refuses to run against a dirty corpus** — this is what
  makes "authored before the run" verifiable rather than a promise.
* Claim-extraction + shuffling implemented as committed code, not discipline (§2.3).
* Scoring rubric and the κ gate in the repo before scoring begins.
* All three arms run; shallow and deep detection reported **separately**, per-class and
  aggregate; false-rejection reported alongside.
* The harness emits `outcome_branch` — selected mechanically by §3's arithmetic — into
  the archived JSON **before** any human writes prose. A human may disagree in the
  writeup, but the machine-selected branch is on record either way.
* Raw JSON archived under `results/` like the F001 re-test.
* **SPEC-41's default gate reconciled.** SPEC-41's DoD makes reproduction on a second
  thinking-capable model the gate for `local_think_false` defaulting on; it ships `True`
  and the gate was never met (senditai has no `qwq` or `deepseek-r1`, and
  `mistral-small3.1` has no thinking channel — which is why it was immune). This spec
  tests only `qwen3.5:9b` and so does **not** discharge that requirement. Either the
  second-model reproduction runs, or SPEC-41 is amended to record that the gate was
  consciously dropped and why.
