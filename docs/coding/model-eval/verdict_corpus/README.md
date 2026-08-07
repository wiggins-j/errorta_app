# SPEC-43 verdict corpus

The review corpus specified by
[SPEC-43 §2.2](../../../specs/SPEC-43-verdict-usefulness-under-think-false.md).

## What it is for

SPEC-43 asks whether a reviewer seat running `think:false` still produces
*useful* verdicts, not merely well-formed ones. Everything measured before it
scored shape (does the JSON parse and match the schema); nothing asked whether
the verdict was right.

Scoring on merge-gate pass rate is degenerate — a reviewer that approves
everything scores 100% — so the oracle here is two-sided: **seeded** items carry
a planted defect that must be caught, **clean** items must pass. Two-sidedness
alone does not close the *salient-surface-anomaly* reviewer (find the single
most locally-odd token, emit its canonical finding, approve otherwise), which is
exactly the strategy a de-reasoned model retains. Hence the depth split:

* **shallow** — the defect is visible inside a single hunk. A careful local
  reader finds it. A surface matcher usually finds it too.
* **deep** — the defect requires composing two facts that are not adjacent: a
  guard deleted in one function that another silently relied on, a lock released
  before the write it protected, an error path that is wrong only given the
  retry accounting introduced elsewhere in the same diff, a comparison whose
  direction is only judgeable from a counter defined in another method. **No
  deep item contains a locally-odd token at the defect site.** This is the
  property the whole corpus exists for; SPEC-43 §4 keys the decision on the deep
  subset.

## Shape

* **N = 32**: 16 seeded + 16 clean.
* Seeded items are 8 defect classes × 2 depths, one item each.
* **Minimal pairs.** Every clean item is the *same base file and the same
  change*, with the defect repaired and nothing else touched. Disjoint seeded
  and clean sets would let a reviewer discriminate on incidental base-file
  features rather than on the defect, and would leave the false-rejection number
  paired with nothing. A seeded item and its twin share a `pair_id`.
* Python throughout. Items are plausible service and library code (40–120 lines
  per post-image) across HTTP handling, caching, file IO, task queues, parsing,
  auth, rate limiting and DB access — deliberately not templated on one shape.
* Several items carry constructs that *look* wrong and are not: an explicit
  `try/finally` close where a `with` would read more naturally (005), a broad
  `except Exception` kept on purpose and justified in a comment (011), a
  hand-rolled minimum search instead of `min(..., key=...)` (014), a mutable
  default argument that is genuinely safe because it is never mutated (003), an
  `x or ()` fallback that is correct for the value it guards (007). These are
  present in *both* twins of the pair, so they are distractors on the seeded
  item and the only thing to find on the clean one. A reviewer that pattern
  matches on style rather than reading for defects will reject clean items on
  them.

## Layout

```
verdict_corpus/
  README.md
  manifest.json          # machine-readable index; the harness consumes this
  removals.md            # drafted-and-discarded items, with reasons
  items/
    001-off-by-one-shallow/
      diff.patch         # exactly what the reviewer is shown
      ground_truth.json
    001-off-by-one-shallow-clean/
      diff.patch         # the minimal-pair twin, defect repaired
      ground_truth.json
    ...
```

Ground truth for a seeded item:

| field | meaning |
|---|---|
| `id`, `pair_id`, `kind` | identity; `pair_id` joins the twins |
| `defect_class`, `depth` | the stratification SPEC-43 §4 reports on |
| `file`, `location` | where the defect lives; for deep items the location is stated *relationally*, because the defect is not at a single line |
| `mechanism` | what actually goes wrong and what it costs — this, not the location, is what a CATCH must match |
| `accepted_finding_forms` | phrasings that count as correct, **authored in advance** |
| `expected_verdict` | `reject` |

Ground truth for a clean item carries `expected_verdict: "approve"` and a `note`
describing what was repaired relative to the seeded twin.

`accepted_finding_forms` deliberately includes **location-vague** phrasings for
the classes where a correct finding is legitimately non-local — race/ordering,
resource lifetime, missing validation. SPEC-43 §2.2 is explicit that these must
not be scored as misses: a reviewer that says "the invalidation added here can
be undone by an in-flight load" has found the defect even though it names no
line.

## How the harness should consume this

1. Read `manifest.json`. `items[]` is the unit of analysis; each entry carries
   `id`, `kind`, `pair_id`, `defect_class`, `depth`, and repo-relative paths to
   `diff` and `ground_truth`.
2. Present `diff.patch` to the reviewer seat through the production
   `coding_turn.v1` envelope and `_review_pr_prompt_segments` — not a
   hand-written toy prompt (SPEC-43 §2.4). The reviewer sees the patch only; it
   never sees `ground_truth.json`.
3. Record the corpus tree SHA in the results JSON and **refuse to run against a
   dirty corpus**. That is what makes "ground truth was authored before the run"
   verifiable rather than a promise (SPEC-43 §6).
4. Score with the three outcomes of §2.3 — `CATCH`, `OTHER-TRUE-POSITIVE`,
   `MISS`. An `OTHER-TRUE-POSITIVE` is excluded from numerator *and* denominator
   and flags the item for repair here; a reviewer that is more correct than the
   corpus must not be scored as a miss.
5. Report shallow and deep detection **separately**, per-class and aggregate,
   with false rejection on the clean twins alongside.

## Provenance

Every item, every `mechanism` and every entry in `accepted_finding_forms` in
this directory was authored by hand **before any model was run against the
corpus**, and lands in its own commit ahead of the harness commit, per SPEC-43
§6. Nothing here was derived from, tuned against, or filtered by a model's
output.

Each seeded item contains **exactly one** planted defect. Both post-images of
every pair were re-read specifically hunting for an unintended second defect;
the three that were found were repaired before the corpus was emitted, and are
recorded in `removals.md`.
