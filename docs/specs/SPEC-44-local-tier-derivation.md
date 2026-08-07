# SPEC-44 — Local tier derivation, a visible bounded downgrade, and a discriminated rung

**Status:** proposed · **Owner:** wiggins-j
**Target version:** v0.1 (engine — `errorta_council/coding/model_tier.py`,
`errorta_council/coding/model_catalog.py`,
`errorta_council/coding/model_assignment.py`,
`errorta_council/coding/autonomy.py`, `errorta_council/coding/runner.py`)
**Relates to:** [SPEC-41](SPEC-41-local-model-integration.md) — this spec **is** SPEC-41
Move 4, extracted so it can ship on its own cadence (SPEC-41 Moves 1-3 landed; Move 4
did not start) · [SPEC-42](SPEC-42-coding-turn-output-budget.md) (landed; the budget
this runs above) · [SPEC-43](SPEC-43-verdict-usefulness-under-think-false.md) · issue
**#82** (all local routes tie at `mid`) · issue **#83** (anchored parameter parsing —
landed, and this spec builds directly on it) · [F127] escalate-up ladder · [F129]
per-task model assignment
**Source of the hardware facts:** `docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md`
(measured on `senditai`, RX 9060 XT 16 GB / 15.9 GiB usable, 15 GiB system RAM)
**Code verified at:** `ae0ffd5`. Every file:line below was read at that commit; SPEC-41's
own citations for this area are stale after the SPEC-42 merge and are re-grounded here.

---

## Problem

### The hard exclusion

`model_selector.select` filters candidates with a **hard exclusion**:

```python
# model_selector.py:71
if effective_rank < requested_rank:
    continue
```

and, when nothing survives, returns a sentinel rather than a weaker model:

```python
# model_selector.py:84-86
if not candidates:
    reason = "unavailable" if not available.intersection(pool) else "no_capable_model"
    return NoCapableModel(reason, tuple(pool))
```

`requested_rank` is `tier_rank(task.difficulty_tier)`, which defaults to `mid`
(`ledger.py:416`, `ledger.py:705`, `schemas.py:84`, `model_assignment.py:87`) and is
`strong` whenever the PM says so (`schemas.py:84` is
`Literal["light","mid","strong"]`).

### Nothing local can ever be `strong`

```python
# model_tier.py:41-42
if rid.startswith(("local.", "fake.")):
    return MID
```

`tier_for_route` returns `MID` for every `local.*` route **before any name
inspection**, and `select` reads `entry.capability_tier`, which comes from
`default_entry` → `tier_for_route` (`model_catalog.py:142`). So on an all-local pool
`tier_rank(...) == 1` for every route, and a `strong` task matches **nothing**.

### What that costs today

`resolve_task_assignment` collapses the sentinel to `None`:

```python
# model_assignment.py:136-137
if isinstance(selected, NoCapableModel):
    return None, override_reason or selected.reason
```

and the sole consumer (`runner.py:6723`, inside the `Assign` handler of
`build_run_turn`) turns that into a **hard blocker**:

```python
# runner.py:6724-6738
if assignment is None:
    store.update_task(task.task_id, state="blocked",
                      model_assignment_failure=override_reason or "no_capable_model")
    store.record_decision(title=f"model assignment failed: {task.title}", ...)
    return TurnOutcome(kind="model_assignment_failed", made_progress=False,
                       hard_blocker=True, reason=override_reason or "no_capable_model", ...)
```

**The reading in the brief is correct, and it is worse than "Move 4 would break
things": it is broken now.** On a local-only deployment a PM that marks one task
`strong` blocks that task permanently, with no rung that can recover it — no model in
the pool can ever outrank `mid`, and no amount of escalation or reassignment changes a
route's `capability_tier`. This is `4.2` of the hardware doc, measured by executing the
real functions: *"A task with `difficulty_tier: "strong"` returns `NoCapableModel` on
an all-local pool. Strong tasks are unservable."*

### And deriving tiers, on its own, makes it worse

Fixing `tier_for_route` alone converts a latent failure into a common one. Under a
`≤8B → light` rule a lone `local.qwen2.5-coder:7b` drops to `light`, and then **every
default-`mid` task** fails the `model_selector.py:71` exclusion. A single-model
deployment — the reference shape, and the one the hardware doc recommends
(`qwen3.5:9b` for all four roles) — stops being able to start work at all.

So the tier derivation and the downgrade fallback are **one change**, not two. Landing
4a without 4b bricks single-model boxes; landing 4b without 4a fixes the `strong` case
and leaves issue #82's dead escalation ladder in place.

### The ladder also loses its reason

`next_escalation_assignment` (`model_assignment.py:149`) has **five** distinct
no-rung outcomes and returns `None` for all of them
(`model_assignment.py:158-159`, `:161-162`, `:175-176`), so `autonomy.py:3306`
cannot tell "the ladder is not applicable" from "the ladder is broken". A ladder that
silently loses a rung reports itself as fully bounded — the SPEC-27 amendment's
complaint, verbatim.

## Principle

> A model that is too weak for the work may still do the work, but never quietly. A
> downgrade is a recorded, bounded, persisted claim about what the run actually ran at
> — and an unavailable ladder rung names which of the five ways it was unavailable.

## What this spec must NOT do

Downgrading difficulty silently is a **correctness hazard**, not a convenience: the
council would do `strong` work on a `light` model and report a clean run. The
following are binding constraints on the implementation, each with its own DoD clause:

1. **No downgrade without a persisted record.** Visibility rides on the
   `ModelAssignment` record itself (`difficulty_downgraded_from`), which is persisted
   with the task, **not** on the `record_decision` write — every `record_decision` in
   this repo is best-effort and several are wrapped in bare `except`. The decision of
   record is the human-readable surface; the assignment field is the guarantee.
2. **Bounded by ranks, and never to the floor by accident.** At most
   `difficulty_downgrade_limit` ranks (default **1**), never below `light`. With the
   default, `strong → mid` and `mid → light` are reachable; `strong → light` is not.
3. **Initial assignment only.** `next_escalation_assignment` gets no fallback.
   Escalation is *supposed* to be able to find nothing; Move 3 makes that legible
   instead of papering over it. The downgrade is an *entry* condition for work, not a
   recovery rung.
4. **Only on `no_capable_model`.** Never on `unavailable` and never on `empty_pool`.
   Lowering the requested tier cannot make an unreachable route reachable, so
   downgrading there would fabricate a capability claim out of a connectivity fault.
5. **Recorded once per assignment, not once per turn.** The reuse guard at
   `model_assignment.py:111-114` re-derives from `task.difficulty_tier`, so a naive
   downgrade fails the guard on every subsequent turn, mints a fresh
   `assignment_id`, and writes a `difficulty_downgraded` decision every single turn.
   Move 2 amends the guard.
6. **`strong` is never derived from a parameter count** (see Move 1).
7. **Nothing changes for hosted routes or for `fake.*`.** `fake.*` keeps returning
   `MID` unconditionally even with the knob on — it is the test seam, and the suite
   relies on fake routes tying.
8. **`escalation_budget_exhausted` is never recorded as an unavailable rung.** It is a
   *walked* rung, already accounted at `c.model_escalations`
   (`autonomy.py:3309`, surfaced to the PM at `autonomy.py:3055` and folded into the
   ladder tuple at `autonomy.py:957`).

---

## Move 1 — derive a local route's tier from its declared parameter count

### 1a. Where the code goes, and why

Issue #83 already landed anchored parameter parsing in `model_catalog`:
`_PARAM_BILLIONS_RE` (`model_catalog.py:95`), `param_billions`
(`model_catalog.py:106`), and the bucket edges `_SMALL_MAX_B = 8.0` /
`_LARGE_MIN_B = 24.0` (`model_catalog.py:102-103`). **Build on those; do not add a
second parser.**

The branch to change lives in `tier_for_route`, which is in `model_tier` — and
`model_catalog` imports `model_tier` (`model_catalog.py:13`), so the parser must move
*down*, not the tier logic up:

* Move `_PARAM_BILLIONS_RE`, `param_billions`, `_SMALL_MAX_B`, `_LARGE_MIN_B` into
  `model_tier` (which today imports only `typing`; it gains `os` and `re` and stays a
  leaf).
* **Re-export all four from `model_catalog` unchanged**
  (`from .model_tier import _LARGE_MIN_B, _SMALL_MAX_B, param_billions`, plus
  `param_billions` in `__all__`). `tests/coding/test_issue83_model_size_hints.py:19-24`
  imports every one of them from `model_catalog`; that file must pass **untouched**.
  It is the regression suite for the substring collision that selected the one model
  that does not fit in 16 GB, and editing it to accommodate a refactor would be exactly
  the wrong trade.

### 1b. The derivation

```python
# model_tier.py
_SIZE_TIERS_ENV = "ERRORTA_LOCAL_SIZE_TIERS"
_OFF = {"", "0", "false", "no", "off"}


def _size_tiers_enabled() -> bool:
    """Read on every call, deliberately uncached: a module-level constant makes
    the knob untestable via monkeypatch and unswitchable without a restart."""
    return os.environ.get(_SIZE_TIERS_ENV, "").strip().lower() not in _OFF


def tier_for_route(route_id: str) -> str:
    rid = (route_id or "").strip().lower()
    if not rid:
        return MID
    if rid.startswith("fake."):
        return MID                       # test seam — never derived
    if rid.startswith("local."):
        if not _size_tiers_enabled():
            return MID                   # today's behaviour, byte for byte
        billions = param_billions(rid)
        if billions is None:
            return MID                   # never assume
        return LIGHT if billions <= _SMALL_MAX_B else MID
    ...                                  # hosted marker matching unchanged
```

Three rules, and the third is the load-bearing one:

* **≤ 8B → `light`**
* **> 8B → `mid`**
* **no declared count → `mid`** (never assume)
* **`strong` is never derived.** A `local.*` route reaches `strong` only through an
  explicit `capability_tier` in `model-catalog-overrides.json`
  (`model_catalog.py:187-191`, applied at `model_catalog.py:231`).

### 1c. Why 8B, checked against the models actually on the box

`ollama list` on senditai carries `qwen3.5:9b`, `qwen2.5-coder:14b`,
`qwen2.5-coder:7b`, `gemma3:27b`, `mistral-small3.1`, `nomic-embed-text`. Under the
rule above:

| Route | `param_billions` | Derived tier | Check against the measurement |
|---|---|---|---|
| `local.qwen2.5-coder:7b` | 7.0 | **light** | 78.7% tests / 45.8% full-task — the weakest generalist measured (§3.1) |
| `local.qwen3.5:9b` | 9.0 | **mid** | 92.3% / 83.3% — the recommended model for all four roles (§Executive summary) |
| `local.qwen2.5-coder:14b` | 14.0 | **mid** | 74.9% / 37.5% |
| `local.gemma3:27b` | 27.0 | **mid** | ~17 GB — does not fit 15.9 GiB (§1) |
| `local.mistral-small3.1:latest` | `None` | **mid** | ~15 GB; the "3.1" is a version, not a size (#83 test line 35) |
| `local.nomic-embed-text:latest` | `None` | **mid** | an embedding model; see "Not fixed" |

**8B is the only edge in the plausible range whose tier ordering agrees with the
measured correctness ordering.** It separates `qwen2.5-coder:7b` (78.7%) from
`qwen3.5:9b` (92.3%) — the one pairwise gap in the pool that the benchmark calls
significant ("The 7b-vs-14b gap is within noise… The `qwen3.5:9b` lead over both is
not"). Push the edge to 10B and the best model in the pool joins the worst at `light`,
which both erases the only signal the benchmark supports and removes the escalation
target. Pull it to 6B and every local model ties at `mid` again — issue #82 unfixed.

It is also **already the number**: `_SMALL_MAX_B = 8.0` is the small/medium
`size_rank` edge from #83, tested at
`test_issue83_model_size_hints.py:105-109`. Reusing it means a route cannot be
`light` on capability and medium on size, which is the drift a second constant invites.

**Why `_LARGE_MIN_B` (24.0) is NOT a `strong` edge.** The only local models a
`≥24B → strong` rule would promote on this box are the ones that do not run on it:
`gemma3:27b` is ~17 GB against 15.9 GiB usable, and the hardware doc is explicit that
system RAM (15 GiB, 4-core i5-6600K) makes a spilled model "not merely slow, it is
unusable" (§1). Because the escalate rung admits only strictly-stronger routes
(`minimum_rank_exclusive`, `model_assignment.py:173`), a count-derived `strong` would
make the ladder's *designed target* the OOM model. Parameter count is an **ordering**
signal; VRAM fit is a **deployment** fact that nothing in the catalog knows. Gating
`strong` on an operator override puts the decision where the fact is. `_LARGE_MIN_B`
keeps its existing job (`size_rank`/`speed_rank` bucketing) and gains no new one.

### 1d. What Move 1 changes beyond selection

`member_tier` / `member_rank` (`model_tier.py:61-73`) also route through
`tier_for_route`, and `member_rank` feeds `member_tiers` (`runner.py:8384-8386`) →
`topology._pick_member` (`topology.py:236-241`), the F127 "reassign to the
highest-tier idle member of the role" rung. Today every local member ties, so that
rung degenerates to index order. With the knob on it starts working on local teams.
This is intended and beneficial, and it is named here because it is a second
behaviour change from one env var.

---

## Move 2 — a bounded, visible difficulty downgrade on the initial assignment path

### 2a. The knob

New field on `CodingAutonomyPolicy` (`autonomy.py`, near the SPEC-41 block at :394):

```python
# SPEC-44: how many capability ranks the INITIAL assignment may drop when the pool
# contains nothing at the requested tier. 0 disables — `resolve_task_assignment`
# returns `(None, "no_capable_model")` and the task hard-blocks, exactly as today.
# 1 (default) permits strong->mid and mid->light; it deliberately does NOT permit
# strong->light, because a two-rank drop is a different claim about the run.
# NEVER applies to the escalation path (`next_escalation_assignment`).
difficulty_downgrade_limit: int = 1
```

Wired the same way as every other int knob:

* `policy_to_dict` (`autonomy.py:451`) gains `"difficulty_downgrade_limit"`.
* The loader gains `difficulty_downgrade_limit=min(2, max(0, int(d.get(
  "difficulty_downgrade_limit", base.difficulty_downgrade_limit))))` — the
  `max(0, …)`-means-disabled convention already used at `autonomy.py:602-605`, with an
  upper clamp of 2 because there are only three tiers.

`model_assignment` has no policy in scope, so the value reaches it the way
`dev_repo_read` and `local_think_false` already do (`runner.py:5688`, `:5700-5701`):

* `build_run_turn(..., difficulty_downgrade_limit: int = 0)` — **default 0 for the
  factory**, matching the `dev_repo_read=False` precedent, so the ~50 direct test
  callers keep legacy behaviour;
* `CodingRunner.run` passes
  `difficulty_downgrade_limit=int(getattr(policy, "difficulty_downgrade_limit", 1))`
  at `runner.py:8388`;
* the `Assign` handler passes it as a keyword to `resolve_task_assignment`
  (`runner.py:6723`), whose new signature is
  `resolve_task_assignment(task, member, *, difficulty_downgrade_limit: int = 0)` —
  again defaulting to the legacy value so `tests/coding/test_model_assignment.py:23`
  and any other two-arg caller is unaffected.

### 2b. The selection change

In `resolve_task_assignment`, replacing `model_assignment.py:131-140`:

```python
satisfied = difficulty
downgraded_from = ""
if not chosen:
    selected = select(pool, available, catalog, difficulty,
                      task_type=task_type, corpus_digest=digest())
    if isinstance(selected, NoCapableModel):
        # ONLY `no_capable_model` is a tier problem. `unavailable` and
        # `empty_pool` are connectivity/config faults; a lower tier cannot fix
        # either, and pretending it did would fabricate a capability claim.
        if selected.reason != "no_capable_model" or difficulty_downgrade_limit <= 0:
            return None, override_reason or selected.reason
        selected, satisfied = _select_downgraded(
            pool, available, catalog, difficulty, task_type=task_type,
            corpus_digest=digest(), limit=difficulty_downgrade_limit,
        )
        if selected is None:
            return None, override_reason or "no_capable_model"
        downgraded_from = difficulty
    chosen = selected.route_id
    rationale = rationale or selected.rationale
    source = "override" if override_reason else "selector"
```

with the helper alongside it:

```python
_TIER_BY_RANK = (LIGHT, MID, STRONG)


def _select_downgraded(pool, available, catalog, difficulty, *, task_type,
                       corpus_digest, limit):
    """Highest tier strictly below `difficulty` that the pool can satisfy, within
    `limit` ranks. Returns (Selection|None, satisfied_tier)."""
    start = tier_rank(difficulty)
    floor = max(0, start - limit)
    for rank in range(start - 1, floor - 1, -1):
        got = select(pool, available, catalog, _TIER_BY_RANK[rank],
                     task_type=task_type, corpus_digest=corpus_digest)
        if not isinstance(got, NoCapableModel):
            return got, _TIER_BY_RANK[rank]
    return None, ""
```

`make_assignment` is then called with `difficulty_tier=satisfied` and a new
`difficulty_downgraded_from=downgraded_from`.

**Why `difficulty_tier` carries the *satisfied* tier and not the requested one.** It is
the tier `select` actually ran at, it is the tier `_effective_rank`
(`model_selector.py:40`) buckets the performance corpus under
(`f"{task_type}:{difficulty}"`), and the reuse guard below compares against it. The
requested tier is not lost — it is `difficulty_downgraded_from`, persisted on the same
record. Storing the requested tier instead would leave the assignment claiming a
capability the route does not have, which is the exact dishonesty this spec exists to
remove.

### 2c. The record

`ModelAssignment` (`model_assignment.py:12-37`) gains one field:

```python
difficulty_downgraded_from: str = ""   # requested tier when this assignment is a downgrade
```

`from_dict` already filters to `cls.__dataclass_fields__` (`model_assignment.py:34-35`),
so old persisted rows load unchanged.

The `Assign` handler (`runner.py:6741-6767`) gains, inside the existing
`assignment.to_dict() != prior_assignment` branch — so it fires exactly when the
assignment is newly written, never per turn:

* `model_difficulty_downgraded_from=assignment.difficulty_downgraded_from or None` on
  the `store.update_task(...)` call;
* a second `store.record_decision(...)` when
  `assignment.difficulty_downgraded_from` is truthy:

```python
store.record_decision(
    title=f"difficulty downgraded: {task.title}",
    context=f"task {task.task_id}", choice="difficulty_downgraded",
    rationale=(
        f"No route in the pool satisfies {assignment.difficulty_downgraded_from}; "
        f"running at {assignment.difficulty_tier} on {assignment.route_id}."
    ),
    related_task_ids=[task.task_id],
    extra={
        "requested_difficulty_tier": assignment.difficulty_downgraded_from,
        "satisfied_difficulty_tier": assignment.difficulty_tier,
        "route_id": assignment.route_id,
        "assignment_id": assignment.assignment_id,
        "pool": pool_snapshot,
    },
)
```

Matching the shape of the neighbouring `model_assigned` record
(`runner.py:6754-6767`) and of `task_model_escalated` (`autonomy.py:3331-3346`).

### 2d. The reuse guard (constraint 5)

`model_assignment.py:111-114` today:

```python
if (existing and existing.member_id == member_id and existing.route_id in available
        and tier_rank(catalog[existing.route_id].capability_tier) >= tier_rank(difficulty)):
    return existing, ""
```

A downgraded assignment fails the last clause forever, because `difficulty` is
re-read from the task each turn. Amend to:

```python
if existing and existing.member_id == member_id and existing.route_id in available:
    have = tier_rank(catalog[existing.route_id].capability_tier)
    if have >= tier_rank(difficulty):
        return existing, ""
    # An already-recorded downgrade for THIS request is honoured rather than
    # re-derived: the route still satisfies the tier the downgrade settled on, and
    # re-minting would write a fresh assignment_id and a duplicate decision every turn.
    if (existing.difficulty_downgraded_from == difficulty
            and have >= tier_rank(existing.difficulty_tier)):
        return existing, ""
```

The guard stays tight in the direction that matters: if the operator later adds a
capable route, `have >= tier_rank(difficulty)` is still false for the weak route, but
the downgrade branch keeps it — accepted, and the remedy is the same as for any stale
assignment (the F127 escalate rung, which now *can* fire under Move 1). Named, not
hidden.

---

## Move 3 — an unavailable rung is recorded with which of five reasons

### 3a. Signature

`next_escalation_assignment(task) -> tuple[ModelAssignment | None, str]`. It has
exactly one production caller (`autonomy.py:3300-3305`) and no direct test callers, so
the change is contained. `""` on success; otherwise:

| Reason | Site today | Meaning |
|---|---|---|
| `no_current_assignment` | `model_assignment.py:158-159` | the task never had an assignment to escalate from |
| `empty_pool_snapshot` | `model_assignment.py:161-162` | no `model_pool_snapshot` on the task |
| `all_routes_attempted` | `model_assignment.py:175` via `select`'s `empty_pool` (`model_selector.py:59`) | every route in the snapshot is already in `attempted_route_ids` |
| `unavailable` | `model_selector.py:85` | candidates remain but none is currently reachable |
| `no_capable_model` | `model_selector.py:85` | candidates are reachable but none outranks the current route — **the #82 case** |

**`all_routes_attempted` is an addition to SPEC-41's four-reason table, and it is the
one a single-model box actually hits.** With a one-route pool,
`candidates = [r for r in pool if r not in attempted]` (`model_assignment.py:165`) is
empty, so `select` returns `NoCapableModel("empty_pool")` at `model_selector.py:58-59`
— never reaching the `no_capable_model` branch. SPEC-41's DoD asserted
`no_capable_model` for that case; it is wrong, and this spec corrects it (see DoD).
Reaching `no_capable_model` requires ≥2 routes where the untried one does not outrank
the current one.

The selector's `empty_pool` is remapped at the escalation seam because at that layer
the pool is not empty — the *candidate* set is, and the difference is the whole point
of the discrimination.

### 3b. Recording

At `autonomy.py:3300-3305`, keeping control flow identical:

```python
if current_escalations < policy.model_escalation_limit:
    next_assignment, rung_reason = next_escalation_assignment(task)
else:
    next_assignment, rung_reason = None, "escalation_budget_exhausted"
```

and, on the `next_assignment is None` path — immediately before the existing
member-exclusion fall-through at `autonomy.py:3348` — a purely additive record:

```python
ledger.record_decision(
    title=f"model escalation unavailable: {task.title or task_id}",
    context=f"task {task_id}", choice="model_escalation_unavailable",
    rationale=(f"No stronger route for {task_id}: {rung_reason}. "
               "Falling through to member exclusion."),
    related_task_ids=[task_id],
    extra={"member_id": member_id, "from_route_id": outcome.member_route,
           "reason": rung_reason,
           "escalation_count": current_escalations,
           "escalation_limit": policy.model_escalation_limit},
)
```

`escalation_budget_exhausted` is recorded with the same `choice` but is *semantically*
a walked rung — the DoD requires a test that asserts it is never conflated with the
four unavailable reasons, and that `c.model_escalations` remains its accounting.

**Move 3 gets no escape hatch, deliberately.** It changes no control flow and no
selection outcome; the only new artifact is a decision record. There is nothing to
restore, so a knob would be a knob with no disable semantics — which this repo's
convention would then require to be tested, against nothing.

---

## Move 4 — the single-model regression test (the primary deliverable)

New file `python/tests/coding/test_spec44_local_tiers.py`. A one-route pool is the
input shape no existing test uses, and it is precisely where the hard exclusion at
`model_selector.py:71` bricks the run.

**The shape must be `model_mode="multi"` with a one-route `model_pool`.** A member with
`model_mode="single"` returns at `model_assignment.py:89-104` and never reaches the
selector at all — a test written that way would assert nothing about this spec. This
trap is stated here because it is the obvious way to write "a single-model deployment"
and it silently passes.

Mandatory cases:

1. **`test_single_local_model_still_assigns_under_derived_tiers`** — pool
   `["local.qwen2.5-coder:7b"]`, `ERRORTA_LOCAL_SIZE_TIERS=1`, default `mid` task,
   `difficulty_downgrade_limit=1`. Asserts: an assignment is returned (not `None`),
   `route_id` is the 7b, `difficulty_tier == "light"`,
   `difficulty_downgraded_from == "mid"`, and no `NoCapableModel` escapes.
2. **`test_single_local_model_downgrade_is_recorded_once`** — drive the `Assign`
   handler twice for the same task; exactly one `difficulty_downgraded` decision and
   one `assignment_id` (constraint 5).
3. **`test_single_local_model_bricks_with_the_knob_disabled`** — same input,
   `difficulty_downgrade_limit=0`: `(None, "no_capable_model")` and a
   `model_assignment_failed` hard blocker. **This is the escape-hatch assertion**: the
   disable value reproduces today's trace exactly.
4. **`test_single_local_model_unchanged_with_tiers_off`** — env var unset: the 7b is
   `mid`, is selected at `mid`, and `difficulty_downgraded_from` is `""`. No decision
   is written. Today's behaviour, byte for byte.
5. **`test_strong_task_on_all_local_pool_downgrades_to_mid`** — the pre-existing
   defect: pool of the real senditai models, task `strong`, tiers **off**. Today this
   returns `NoCapableModel`; after this spec it runs at `mid` with the record.
6. **`test_escalation_rung_reason_on_single_model_pool`** — `all_routes_attempted`,
   not `no_capable_model`.

Beyond the single-model shape:

7. Real-pool tier table (the six routes in Move 1c), asserting the **documented**
   answers, so a future regex change cannot silently reclassify them. Mirrors the
   table-driven style of `test_issue83_model_size_hints.py:28-42` and uses the same
   real ids.
8. `local.gemma3:27b` is **not** `strong` without an override; with a
   `model-catalog-overrides.json` entry it is.
9. `fake.*` is `mid` with the knob on and off.
10. A mixed local pool yields >1 distinct rank, and an exhausted `light` task escalates
    from the 7b to the 9b.
11. Each of the five rung reasons has its own case, plus
    `escalation_budget_exhausted`.
12. `test_issue83_model_size_hints.py` passes **unmodified** after the parser move.

---

## Move 5 — documentation

* **`docs/coding/PM_REFERENCE.md`** — `difficulty_downgrade_limit` in the "Reliability
  guards" paragraph (around line 226, next to `model_escalation_limit`), **and** in the
  machine-readable contract block (`"difficulty_downgrade_limit": 1`, keeping the
  block's alphabetical order — between `delivery_review_round_limit` and
  `diff_deadlock`). `tests/coding/test_f145_pm_reference.py` asserts
  `contract["autonomy_defaults"] == policy_to_dict(CodingAutonomyPolicy())`, so a
  missing entry fails the suite.
* **`docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md`** — §4.2's "Recommended" becomes a
  pointer to this spec, and `model-catalog-overrides.json` is documented as **required
  configuration for a local-only team** (per issue #82), not as a debugging tool: it is
  the only route to `strong`, the only remedy for the parse limits below, and the only
  reset for a corpus-demoted route.

---

## Escape hatches

Each disable value reproduces today's behaviour **exactly**, and each is asserted by
its own test.

| Knob | Where | Default | **Disable value** | What the disable value restores |
|---|---|---|---|---|
| `ERRORTA_LOCAL_SIZE_TIERS` | env, read per call in `model_tier._size_tiers_enabled` | unset (off) | **unset**, or `""`/`0`/`false`/`no`/`off` | `tier_for_route` returns `MID` for every `local.*` route before any name inspection — `model_tier.py:41-42` unchanged |
| `difficulty_downgrade_limit` | `CodingAutonomyPolicy` → `build_run_turn` → `resolve_task_assignment` | `1` (policy); `0` on the `build_run_turn` factory, for the ~50 direct test callers | **`0`** | `resolve_task_assignment` returns `(None, selected.reason)` at `model_assignment.py:136-137`; the task hard-blocks with `model_assignment_failed` at `runner.py:6724-6738` |
| `model-catalog-overrides.json` | `model_catalog.py:166` | absent | **absent** | per-route `capability_tier`/`cost_tier`/`size_rank`/`speed_rank`; the only route to a `strong` local model and the operator's reset |
| *(Move 3)* | — | — | n/a | no control-flow change; nothing to restore |

---

## Risks

**Move 1 changes model selection on every existing local deployment that turns it on.**
Today all local routes tie at `mid` (`model_tier.py:42`); afterwards a `7b` becomes
`light` and stops being selected for the mid-difficulty work it currently receives.
Mitigations: default off, Move 2's fallback, and the tie-break ordering asserted in
tests.

**A default of `1` for `difficulty_downgrade_limit` changes hosted deployments too.**
A `strong` task on a pool with no strong route today hard-blocks; afterwards it runs at
`mid` with a recorded downgrade. This is the intended fix (the hard block is a bricked
run, not a safety property), but it is a behaviour change beyond local pools and it is
stated here rather than buried. `0` restores the block.

**The corpus demotion asymmetry gets sharper.** `_effective_rank`
(`model_selector.py:43-44`) **demotes** a route by one rank on a poor record and
nothing ever promotes one. Under Move 1 a `light` local route that is demoted floors at
rank 0 and can be excluded from `mid` work permanently. Move 2's fallback is what keeps
that from bricking the box, and the `difficulty_downgraded` record is what makes the
state legible. Promotion is a corpus-semantics change with its own evidence bar and is
out of scope.

**Tie-breaking still prefers the measured-worse model.** With `qwen3.5:9b` and
`qwen2.5-coder:14b` both at `mid`, both at `cost_tier=0`, and both at
`size_rank=speed_rank=1`, `select`'s key (`model_selector.py:75-82`) falls through to
`entry.route_id` — and `local.qwen2.5-coder:14b` sorts before `local.qwen3.5:9b`, so
the selector picks the model that scored 74.9% over the one that scored 92.3%. This
spec does not fix it (a name-derived tier cannot encode a benchmark), does not make it
worse, and names `model-catalog-overrides.json` as the remedy.

---

## Definition of done

**Move 1 — derivation**

- `param_billions`, `_PARAM_BILLIONS_RE`, `_SMALL_MAX_B`, `_LARGE_MIN_B` live in
  `model_tier` and are re-exported from `model_catalog`;
  `tests/coding/test_issue83_model_size_hints.py` passes **with zero edits**.
- With `ERRORTA_LOCAL_SIZE_TIERS` unset, `tier_for_route` returns `mid` for all six
  real senditai routes — identical to today.
- With it set, the six real routes classify exactly as the Move 1c table states, and a
  table-driven test asserts the documented answer per route.
- `local.gemma3:27b` is `mid`, never `strong`, with the knob on; adding a
  `capability_tier: "strong"` override makes it `strong` and nothing else does.
- `fake.*` returns `mid` with the knob on and with it off.
- On a mixed local pool, `tier_for_route` yields more than one distinct rank, and an
  exhausted task escalates from `local.qwen2.5-coder:7b` to `local.qwen3.5:9b`.

**Move 2 — the downgrade**

- **Single-model deployment test (mandatory, and the gate for this spec).** A
  `model_mode="multi"` member with a pool of exactly `["local.qwen2.5-coder:7b"]`,
  `ERRORTA_LOCAL_SIZE_TIERS` on, and a default-`mid` task still assigns that route,
  sets `difficulty_tier="light"` and `difficulty_downgraded_from="mid"`, and lets no
  `NoCapableModel` reach the caller.
- The same input with `difficulty_downgrade_limit=0` returns
  `(None, "no_capable_model")` and hard-blocks — today's trace exactly.
- The same input with `ERRORTA_LOCAL_SIZE_TIERS` unset assigns at `mid` with
  `difficulty_downgraded_from == ""` and writes no `difficulty_downgraded` decision.
- A `strong` task on an all-local pool (tiers off) assigns at `mid` and records the
  downgrade — the defect measured in §4.2 of the hardware doc is closed.
- Driving the `Assign` handler twice on a downgraded task yields exactly one
  `difficulty_downgraded` decision and one `assignment_id` — no per-turn churn.
- `difficulty_downgraded_from` is readable from the **persisted task record**, not only
  from the decisions log, so visibility does not depend on a best-effort write.
- With `difficulty_downgrade_limit=1`, a `strong` request on a pool whose only route is
  `light` returns `None`, not a `light` assignment — the two-rank drop is refused.
- `NoCapableModel("unavailable")` and `NoCapableModel("empty_pool")` never trigger a
  downgrade, asserted by their own cases.
- `next_escalation_assignment` never downgrades, asserted directly.

**Move 3 — the rung**

- With a single-route pool the escalate rung records **`all_routes_attempted`** (this
  supersedes SPEC-41's DoD clause, which named `no_capable_model` for this shape and is
  incorrect — `model_selector.py:58-59` returns `empty_pool` before the candidate loop).
- Each of `no_current_assignment`, `empty_pool_snapshot`, `all_routes_attempted`,
  `unavailable`, `no_capable_model` is produced by its own case.
- `escalation_budget_exhausted` is recorded when and only when
  `current_escalations >= policy.model_escalation_limit`, is never one of the five
  above, and leaves `c.model_escalations` as its accounting.
- The fall-through to member exclusion at `autonomy.py:3348` is byte-for-byte
  unchanged in behaviour; the record is purely additive.

**Move 5 — docs**

- `difficulty_downgrade_limit` appears in `docs/coding/PM_REFERENCE.md` prose **and** in
  the contract JSON block; `tests/coding/test_f145_pm_reference.py` passes.
- `docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md` §4.2 points at this spec and names
  `model-catalog-overrides.json` as required configuration for a local-only team.

**Escape hatches**

- Every knob's disable value has a test asserting it reproduces today's behaviour
  exactly, per the table above.

---

## Out of scope, and failure modes NOT fixed

- **A VRAM-fit check.** Nothing in the catalog knows the card. Refusing to derive
  `strong` is a *mitigation* of that gap, not a fix: escalating into a model that will
  not fit remains reachable through an operator override, and on a 16 GB box
  `gemma3:27b` and `mistral-small3.1` are both in that category. Belongs with model
  availability.
- **The parameter-name parsing limits (SPEC-41 4d), unchanged and re-stated.**
  `local.mixtral:8x7b` (47B) and `local.llama4:16x17b` (108B) parse to `None`;
  `local.qwen3-coder:30b-a3b` parses to 30.0 despite 3B *active*;
  `local.mistral-small3.1:latest` (24B) parses to `None`. All land on `mid`, which is
  the correct failure direction, and none is fixable by a better regex — a model id is
  not a spec sheet. `model-catalog-overrides.json` is the remedy.
- **An embedding model in the pool.** `local.nomic-embed-text:latest` derives `mid` and
  is selectable for coding work. That is true today (everything is `mid`) and this spec
  does not make it worse; it is an operator-configuration error with an override remedy.
- **Promotion in `_effective_rank`.** The demote-only asymmetry
  (`model_selector.py:43-44`) is named in Risks and left alone.
- **The 14b-before-9b tie-break.** Named in Risks; a name-derived tier cannot encode a
  benchmark result.
- **Calibrating what the PM *means* by `strong`.** Difficulty tiers are assigned by a
  model against no local-hardware baseline; this spec makes the mismatch between a
  requested tier and an available one visible and bounded, and says nothing about
  whether the request was well-founded.
- **`metadata.model_tier` as a selection override.** It is not one, contrary to an
  earlier draft of SPEC-41: `member_tier` (`model_tier.py:61-70`) reads it, but `select`
  reads `ModelCatalogEntry.capability_tier` (`model_selector.py:38`), which comes from
  `default_entry`/`tier_for_route` and is overridable only through
  `model-catalog-overrides.json`. `metadata.model_tier` governs the F127 role-tier
  comparisons, not model selection.
- **Local models served over the `custom` provider.** They dispatch through
  `_registry_dispatch` (`gateway_local.py:253`) and are not `local.*` routes, so no
  tier is derived for them.
