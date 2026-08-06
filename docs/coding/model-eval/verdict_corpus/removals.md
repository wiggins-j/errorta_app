# Removals and repairs

SPEC-43 §2.2 splits the removal rule in two, and this log honours the split:

* **arguable ground truth** — "is this even a defect?", or "is this the defect
  the item claims?" → **remove**.
* **arguable phrasing** — "a correct finding could be worded several ways" →
  **keep**, and enumerate the wordings in `accepted_finding_forms`. Nothing in
  this log was removed for phrasing; that is what the location-vague entries in
  the race/ordering, resource-leak and missing-validation items are for.

## Removed

### R1 — resource-leak, deep: connection re-acquired inside a retry loop
Draft for what is now pair 006. A `execute_with_retry` helper acquired a fresh
connection on each attempt without releasing the previous one, exhausting the
pool. **Removed: not actually deep.** A bare `pool.acquire()` inside a `for
attempt in ...` loop is itself a locally-odd token — a surface matcher finds it
without composing anything, which defeats the only property the deep subset
exists to measure. Replaced by the cursor-cache/`discard` composition.

### R2 — wrong-comparison-operator, deep: kilobyte/byte unit mismatch in eviction
An `_entry_cost()` returning kilobytes compared against a byte-denominated
`max_bytes` in a different method, so the cache held ~1024× its configured
limit. Genuinely deep and a genuine defect, but **removed: arguable ground
truth** — for the *class label*. It is a unit-mismatch defect, not a
wrong-operator defect, and an item whose class assignment is arguable
contaminates the per-class breakdown SPEC-43 §4 says governs the decision.

### R3 — wrong-comparison-operator, shallow: `<=` in a minimum-supported-version gate
Rejecting clients on exactly the minimum supported version. **Removed: arguable
ground truth by overlap.** A boundary-inclusive comparison is an off-by-one as
readily as it is a wrong operator; a correct finding would have been
indistinguishable from pair 001's, and the two classes would no longer be
measuring different things. Replaced by an inverted-direction mtime test (013),
which is a direction error and not a boundary error.

### R4 — off-by-one, shallow: `tail(n)` returning `n-1` samples from a ring buffer
**Removed: arguable ground truth.** The helper had no caller in the diff that
made the short return observably wrong, so "is this a defect or an unimportant
slice convention?" was a fair question. Replaced by the bind-parameter batching
in 001, where the consequence is a hard driver error.

### R5 — swallowed-exception, deep: refresh failure that also reset a backoff timer
Draft for what is now pair 012. The `except AuthError` branch both returned a
stale token *and* left a backoff timer reset in another method, so the failure
never engaged backoff. **Removed: two candidate defects.** Ground truth could
not say which one "the" defect was, and a reviewer naming either would have to
be scored CATCH — which makes the item unable to discriminate. Rewritten so the
single defect is the missing expiry check on the fallback, made wrong by the
precondition of the calling path.

## Skew

**Three of the five removals (R1, R2, R5) are deep items, and both remaining
removals are shallow.** This is itself a finding about corpus difficulty and is
recorded here rather than smoothed over: deep items are markedly harder to
author cleanly, because the two properties that make an item deep — no locally
odd token, and a defect that only exists as a *relationship* between two places
— pull directly against the two properties that make ground truth defensible —
exactly one defect, and an unarguable class label. Every deep removal here was
for one of those two collisions, never for the defect being unreal.

The practical consequence for SPEC-43: if the deep subset turns out to be at
floor for all three arms, "the corpus is too hard" is a live explanation, and
§4's `gemma3:27b` reference pass is what has to settle it. If it turns out to be
at ceiling, the honest reading is that authoring pressure pushed the deep items
shallower than intended.

## Repairs (kept, not removed)

Found during the mandated re-read of every post-image for an *unintended second*
defect. Each was repaired in **both** twins before the corpus was emitted, so no
item ships with a second defect the ground truth does not name.

* **001** — the `write()` docstring claimed the batched write was one atomic
  transaction, which the code does not guarantee (no rollback on a mid-write
  failure). A reviewer flagging that would have been correct and scored MISS.
  Docstring rewritten to claim only what the code does: one deferred commit.
* **006** — `stats()` summed a generator over `self._cursors.values()` under the
  pool lock while `prepared()` mutated the same dict without it: a real
  "dictionary changed size during iteration" race in both twins, unrelated to
  the planted leak. `stats()` now takes `len()` (atomic), and `prepared()`
  documents why it needs no lock (a connection is held by one thread at a time;
  the outer `setdefault` is atomic).
* **016** — `handle()` used `event.get("id")` as a dedup key without rejecting a
  missing id, so two id-less events would have deduplicated against each other.
  Both twins now reject an event with no id with a 400.

## Residual uncertainty

Two places where "exactly one defect" rests on a judgement rather than on
certainty, recorded so a scorer can see them coming:

* **003** — `self._snapshot` is rebound from the refresh thread without a lock.
  This is a single atomic attribute rebind onto a frozen dataclass and is safe
  in CPython, but a reviewer could reasonably raise it. It is pre-existing in
  spirit (the base file has the same pattern in `load_from_disk`) and is present
  in both twins. If a run flags it, score `OTHER-TRUE-POSITIVE`, not `CATCH`.
* **012** — `TokenCache` has no lock at all, so two threads can refresh
  concurrently. This is inherited from the base file and unchanged by the diff,
  and it is present in both twins. Same instruction: `OTHER-TRUE-POSITIVE`.
