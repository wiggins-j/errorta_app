# SPEC-41 — Local-model integration: truncation honesty, thinking, constrained decoding, tiers

**Source:** The RX 9060 XT benchmark (`docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md`)
and issues #81, #82, #84, plus the `/api/chat` follow-up validation in §4.1a of that
doc. Issue #83 already landed separately.
**Target version:** v0.1 (engine — `errorta_council/gateway_local.py`,
`errorta_council/scheduler.py`, `errorta_council/coding/runner.py`,
`errorta_council/coding/autonomy.py`, `errorta_council/coding/model_tier.py`,
`errorta_council/coding/model_catalog.py`,
`errorta_council/coding/model_assignment.py`)
**Depends on:** [SPEC-42](SPEC-42-coding-turn-output-budget.md) — **hard dependency**,
see "Sequencing".
**Relates to:** [SPEC-27](SPEC-27-convergence-as-control.md) (the amendment this
implements) · [F001](F001-judge-and-grounding-loop.md) (whose judge recommendation
this undercuts)
**Status:** proposed · **Owner:** wiggins-j

---

## Problem

Measured defects in how Errorta talks to local models. All were found on `senditai`
(Ubuntu + ROCm, RX 9060 XT 16 GB), the reference box.

### The two egress paths, and which one each defect lives on

Every claim below is scoped to a path, because the two do not share code:

* **Council-room path.** `scheduler.py` builds `LocalCouncilModelRequest` at **eight**
  sites: `scheduler.py:1624`, `:2215`, `:2395`, `:2676`, `:2754`, `:2952`, `:3607`,
  `:3748`.
* **Coding-council path.** Not one of those eight. Every coding turn goes through
  `coding/runner.py:7583` `gateway_member_caller`, whose inner `caller`
  (`runner.py:7592`) builds its own request at `runner.py:7616`. The coding turn
  *itself* is dispatched from exactly **six** model-call sites, all inside
  `build_run_turn` (`runner.py:5297`): `runner.py:5433`, `:5458` (coding turn +
  corrective retry), `:5508`, `:5526` (PM governance), `:5635`, `:5654` (governance
  review).

Both paths converge on `LocalGateway.call` (`gateway_local.py:191`), which for a
`local.*` route dispatches to `_ollama_dispatch` (`gateway_local.py:337`) and posts
`/api/chat`.

### The evidence that reframes #81

The original measurement used `/api/generate` and concluded that `format: "json"`
retargets the JSON constraint onto the thinking channel, emptying `response`. Neither
path uses that route, and re-measuring on `/api/chat` gives a different mechanism and
a different fix. Six trials per arm, cache-busted, **one model (`qwen3.5:9b`), one
prompt shape (a reviewer verdict), contended GPU**:

| Arm | schema ok | `done_reason` | mean `eval_count` |
|---|---|---|---|
| `num_predict=800`, thinking **on** | 1/6 | `length` ×5, `stop` ×1 | 727 |
| `num_predict=8192`, thinking **on** | 4/6 | `stop` ×6 | 2197 |
| `num_predict=800`, **`think:false`** | **6/6** | `stop` ×6 | **33** |

Two separable failures, both real:

1. **Truncation.** `qwen3.5:9b` emits ~1,950 tokens of reasoning before answering, so
   any budget under ~2,000 cuts it off mid-thought. Raising the budget removes the
   *truncation* completely (`done_reason` goes `length`→`stop` 6/6). It does **not**
   fix schema compliance: the 8192 arm is still 4/6. "Raising the budget fixes it"
   is true of truncation only.
2. **Schema compliance and cost.** Independent of budget: 4/6 with thinking vs 6/6
   without, and **2197 → 33 generated tokens** per reviewer verdict — a ~66×
   reduction. On a single local GPU that is a ~100 s turn becoming near-instant.

**Evidence caveat, stated here and not only in the source doc.** n=6 per arm, one
model, one prompt shape, on a GPU with other load. The 6/6 result is a strong signal
about *this* model on *this* turn shape and nothing more. `local_think_false` must not
default on until it is reproduced on a second thinking-capable model — `deepseek-r1`
or `qwq` — on the same reviewer-verdict shape (see Definition of done).

### The budget the coding council actually sends — correcting an earlier claim

An earlier draft of this spec asserted the council already sends 8192. It does not.
`runner.py:7622` hardcodes `max_output_tokens=int(tl.get("max_output_tokens", 2048)
or 2048)` and never consults `_is_reasoning_model` (which exists only in
`scheduler.py:149`, used at `scheduler.py:1744` and `:1750` — the Council-room path).
The 8192 arm above is the **harness**, not the council. So on the reference box the
coding council sits *below* the ~2,000-token reasoning preamble and truncates by
construction. **That defect is SPEC-42's, not this spec's** — see Sequencing.

### The dangerous part is what the gateway does with a truncation

When `content` is empty, `gateway_local.py:390-393` substitutes
`THINKING_TRACE_MARKER + thinking` — so the caller receives a *reasoning trace
presented as an answer*. That is exactly how a truncated thinking model surfaces as
F001's "wrong-schema JSON". The workaround treats the symptom and disguises the cause.

### #84 — no `format` is ever sent

The `/api/chat` body (`gateway_local.py:341-349`) carries only `model`, `messages`,
`stream`, and `options{num_predict, temperature}`. Models emit valid JSON but
fence-wrapped **0/6** of the time without `format`, so every structured turn depends
on extraction heuristics succeeding.

### #82 — local routes cannot escalate

`model_tier.tier_for_route` returns `MID` for every `local.*`/`fake.*` route before any
marker matching (`model_tier.py:42`), and `model_assignment.next_escalation_assignment`
(`model_assignment.py:146`) selects with `minimum_rank_exclusive=current_rank`
(`:170`). On an all-local pool nothing can be strictly stronger, so `select` returns
`NoCapableModel` and the rung returns `None` (`model_assignment.py:172-173`) — and
`None` is indistinguishable from "never applicable", so a ladder that silently loses a
rung still reports itself as fully bounded (see the SPEC-27 amendment).

## Principle

> A local model's failure must be reported as the failure it is. A truncation is not
> an answer, an unavailable ladder rung is not a walked rung, and a model's declared
> size is not a guess.

## Sequencing

The moves are ordered by dependency and by blast radius, and each must land and be
observed before the next starts:

0. **SPEC-42 (dependency, not this spec).** Raise the coding turn's output budget past
   the reasoning preamble. Until `runner.py:7622` stops hardcoding 2048, a
   thinking-capable local model truncates on every structured turn and moves 1–3
   are measured through a defect SPEC-41 does not own.
1. **Move 1 — truncation honesty.** No dependency; it is the safety fix. It must
   precede moves 2–3 so that any regression they cause surfaces as a labelled
   truncation rather than as a disguised one.
2. **Move 2 — `think:false` on structured turns.**
3. **Move 3 — `format:"json"`,** which is *gated on move 2* (see I1 below); it cannot
   be enabled independently.
4. **Move 4 — local tiers.** Last, and behind its own knob, because it changes model
   selection on every existing local deployment.

## The plumbing problem, solved once

Three of the four moves need a policy decision to reach a point that has no policy in
scope. Rather than invent a mechanism, this spec reuses the one already proven in this
exact seam by `repo_read_root`:

| Stage | Existing precedent | SPEC-41 |
|---|---|---|
| Policy field | `CodingAutonomyPolicy.dev_repo_read` (`autonomy.py:131`+), read via `load_policy(store)` (`autonomy.py:938`) | `local_think_false`, `local_structured_format`, `local_truncation_guard`, `local_size_tiers` |
| Threaded into the turn factory | `build_run_turn(..., dev_repo_read=False, reviewer_repo_read=False)` (`runner.py:5305-5306`), default `False` so the ~50 direct test callers keep legacy behaviour; `CodingRunner.run` passes `policy.X` | same keyword-argument shape, same `False` defaults |
| Tagged on a per-turn member copy | `dev_member = {**member, "repo_read_root": str(repo_root)}` (`runner.py:6391`, `:6697`, `:6990`, `:7355`) | `{**member, "structured_output": True}` at the six call sites `runner.py:5433/5458/5508/5526/5635/5654` |
| Read back at the gateway seam | `gateway_member_caller` reads the member key and forwards it as request metadata (`runner.py:7611-7615`) | reads `structured_output`, forwards `metadata["structured_output"]` |
| Consumed at egress | `_ollama_dispatch` / provider reads `request.metadata` | `_ollama_dispatch` reads `request.metadata` |

This resolves the design contradiction the review found. `gateway_member_caller`'s
signature is `(member, prompt) -> str` (`MemberCaller`, `runner.py:86`), so turn shape
is genuinely not knowable *inside* it — but it is knowable at the six sites that call
it, every one of which immediately feeds `parse_coding_turn` / `parse_governance_turn`
and therefore *is* a structured turn by construction. The flag is set where the shape
is proven, not inferred from role, and the gateway still only honours a flag.

**Alternatives considered and rejected:**

* *Set it at the ~50 `run_turn` dispatch sites.* Wrong count and wrong layer — those
  callers hand `run_turn` an `action`, not a member; the six model-call sites are the
  only places a member and a prompt exist together.
* *Infer from role in the gateway.* Forbidden by invariant 3's intent: `gateway_local`
  is the sole egress and is deliberately ignorant of coding-role semantics.
* *Add a policy parameter to `gateway_member_caller`.* It has ten call sites in
  production and scripts (`runner.py:7583` def; `coding/wizard.py:257`;
  `errorta_app/routes/coding.py:1885`, `:2173`, `:2660`, `:2806`, `:4436`;
  `scripts/validate_f127_live.py:80`, `scripts/validate_f087_live.py:139`,
  `scripts/demo_calculator_live.py:85`), none of which has a `CodingAutonomyPolicy`.
  Threading one through all ten to serve two booleans is a worse trade than the
  member-dict tag the codebase already uses.
* *Import the policy inside `gateway_local`.* `gateway_local` currently imports only
  `json`, `time`, `zlib`, `dataclasses`, `typing`, `httpx`
  (`gateway_local.py:6-14`); `coding` imports `gateway_local`, so the reverse edge is
  a cycle. **Correction to the review's premise:** `tests/council/test_import_lint.py`
  does *not* enforce a coding-import ban — it enforces `FORBIDDEN_TOP_LEVEL_MODULES`
  (provider SDKs, line 8) and httpx-only-in-gateway (lines 20, 57). The ban is
  architectural (invariant 3, `gateway_local.py:1`) and a cycle, not a lint. If this
  spec's design is accepted, the import-lint test **should** gain a
  `gateway_local must not import errorta_council.coding` case, and this spec adds it.

**The Council-room path** needs the same tag on its own eight request sites
(`scheduler.py:1624/2215/2395/2676/2754/2952/3607/3748`); those already build
`LocalCouncilModelRequest` directly and already populate `metadata`, so the tag is a
one-key addition per site with no plumbing. Moves 2 and 3 land on both paths.

**Move 4's knob has no such path and therefore does not get one.** `tier_for_route`
(`model_tier.py:37`) is a pure leaf reached via `default_entry`
(`model_catalog.py:113-114`) ← `load_catalog` (`model_catalog.py:195`), whose six
callers (`scheduler.py:3415`, `runner.py:2383`, `runner.py:7223`,
`model_assignment.py:106`, `:165`, `performance_corpus.py:202`) have no policy either.
So `local_size_tiers` is **not** a `CodingAutonomyPolicy` field: it is an environment
variable read inside `model_catalog` (`ERRORTA_LOCAL_SIZE_TIERS`, default off),
alongside the per-route escape hatch that already exists — see Move 4.

## What this spec does

### Move 1 (#81b) — a truncation is loud, not disguised

Read `done_reason` from the `/api/chat` response in `_ollama_dispatch`. When it is
`"length"`:

* **Suppress the `THINKING_TRACE_MARKER` substitution** (`gateway_local.py:390-393`).
  The marker exists for a *genuine* thinking-only response (`done_reason == "stop"`,
  empty `content`); using it for a truncation is what disguises the defect. A genuine
  thinking-only response keeps the marker unchanged.
* Add `truncated: bool = False` to `LocalCouncilModelResult` (`gateway_local.py:63-79`)
  and set it. Return `content` as-is — empty when the model never reached its answer,
  clipped when it did.
* **Do not raise.** See below.

**Why not `RetryableError`.** The obvious move — `raise RetryableError(
"local_output_truncated")` — converts a silent-wrong into a guaranteed stall-then-halt,
and the review's suggested remedy does not avoid it:

1. A raised gateway exception is caught by the capturing wrapper
   (`runner.py:5371-5383`), classified by `classify_member_failure`
   (`member_health.py:177`), and re-raised as `_MemberCallFailed`.
2. `run_turn` turns that into `TurnOutcome(kind="member_failed", made_progress=False)`
   (`runner.py:7100-7104`). That outcome does **not** set `unproductive=True`, so
   `_handle_unproductive` (`autonomy.py:3150`, dispatched at `autonomy.py:3866-3867`)
   is never reached. The F127 escalate ladder is not on this path at all.
3. It goes to `_member_health_stop` instead (`autonomy.py:3417-3423`), which caps at
   `classify_aware_cap(failure.status, policy)` and returns `MEMBER_UNHEALTHY`
   (`autonomy.py:3850-3857`) — a blocking member-health Problem that stops the run.
4. **Correcting the review's proposed fix:** rewording the message to match the
   `unparseable` patterns at `member_health.py:163-171` does **not** help.
   `classify_aware_cap` (`member_health.py:196-208`) returns
   `policy.member_failure_limit` (default 3, `autonomy.py:141`) for `unparseable` and
   `errored` alike; only `_TERMINAL_STATUSES` cap at 1. UNPARSEABLE and ERRORED reach
   the *same* halt at the *same* count. Because truncation is deterministic, either
   spelling is 3 identical ~100 s attempts and then a halt.

**What happens instead.** Returning the (possibly empty) content lets the *existing*
turn-parse path own it: `parse_coding_turn` fails, the corrective-retry loop runs
(`runner.py:5441-5458`), and a persistent failure returns
`TurnOutcome(kind="noop", unproductive=True, reason=parsed.code.value)`
(`runner.py:6419-6423`) — which is precisely the F127 escalate-up ladder the review
wanted, reached by the door that actually opens onto it. The turn is bounded by
`worker_unproductive_limit` and escalates the *model*, not the *run*.

**Making `truncated` observable on the coding path.** `MemberCaller` is
`Callable[[dict, str], str]` (`runner.py:86`) and stays that way. `truncated` crosses
the string seam via the thread-local usage sink built for exactly this problem
(`_usage_sink`, `runner.py:134`, documented at `runner.py:127-133`): the caller writes
`_usage_sink.last` at `runner.py:7671`; the capturing wrapper clears it at
`runner.py:5366` and folds it in at `runner.py:5391`. Move 1 adds one key,
`"truncated"`, to that dict, and `record_turn` persists it so a truncated turn is
visible in the transcript rather than inferred. **No `MemberCaller` signature change.**

**Wiring `truncated` into the Council-room rejection gates (the marker's other job).**
Three gates key on `is_thinking_burn` (`gateway_local.py:411` sets it from the marker
prefix). Suppressing the marker on a truncation therefore *weakens* them, so the same
change strengthens them:

| Gate | Today | After Move 1 |
|---|---|---|
| `scheduler.py:2780` credibility judge — `if text and not is_thinking_burn` | rejects empty text and marker text | also rejects `result.truncated` (a clipped non-empty verdict is not a verdict) |
| `scheduler.py:2974` synthesis — `if is_thinking_burn or not content.strip(): return None` | rejects both | also rejects `result.truncated` |
| `scheduler.py:3954` answer-of-record — `if not result.is_thinking_burn` | accepts empty content, and would accept clipped content | rejects when `result.truncated` **or** content is blank |

`scheduler.py:3954` is the one that regresses without this: with the marker suppressed
and content empty it would record an empty string as the answer of record, and with
content clipped it would record a half-finished answer as a complete one.

### Move 2 (#81a) — `think: false` on structured turns

`_ollama_dispatch` adds `"think": false` to the `/api/chat` body when
`request.metadata.get("structured_output")` is true. The gateway stays dumb: it
honours a flag, it does not infer turn semantics from the role. The flag is set as
described in "The plumbing problem, solved once".

**Open item — CLOSED, measured 2026-08-06 on the reference box.** Ollama's tolerance
of `think` on a *non-thinking* model was not established from this repo, so it was
measured directly against `senditai`:

```
POST /api/chat  {"model": "...", "think": false, ...}
qwen2.5-coder:7b   HTTP 200   content='OK'
gemma3:27b         HTTP 200   content='OK\n'
```

Both non-thinking models accept the field and answer correctly — Ollama ignores it
harmlessly. **So the flag is unconditional on `structured_output`**, and the fallback
(a thinking-capable marker table local to `gateway_local`) is NOT needed. That
fallback is dropped from this spec rather than carried as dead contingency.

Scope of the claim: two models, one Ollama version, `/api/chat` only. A future Ollama
that rejects the field would surface as a `gateway_4xx` on every structured local
turn — loud and immediate, not silent — which is an acceptable failure mode for a
field this cheap to remove.

### Move 3 (#84) — `format: "json"` on structured turns, **coupled to Move 2**

`_ollama_dispatch` adds `"format": "json"` when `structured_output` is set.

**The knobs are not independent, and an earlier draft was wrong to require that they
be.** Issue #84's warning is that constraining the output channel while the thinking
channel is live is the combination that empties `content`. "Independently switchable"
makes exactly that combination reachable and a DoD bullet then *required* it. Instead:

> `format:"json"` is sent **only when thinking is known to be off for that request** —
> i.e. `local_structured_format` is on **and** (`think:false` was sent for this
> request **or** the route is not thinking-capable).

So `local_think_false=False` implies no `format`, by construction and not by
convention. `local_structured_format=False` still disables `format` alone. The
combination the issue warns about is unreachable, and a test asserts it.

### Move 4 (#82) — local tiers, gated; and an observable rung

This is the highest-blast-radius move and the one most likely to make a working
deployment stop working. It ships last, off by default, with a mandatory
single-model test.

**4a. Tier derivation.** `param_billions` moves from `model_catalog`
(`model_catalog.py:78`) to `model_tier`. `model_tier` is a leaf (it imports only
`typing`) and `model_catalog` already imports from it (`model_catalog.py:114`), so
this inverts the dependency correctly instead of creating a cycle. When
`ERRORTA_LOCAL_SIZE_TIERS` is set, `tier_for_route` derives a `local.*` route's tier
from its declared parameter count:

* **≤ 8B → `light`**
* **> 8B → `mid`**
* **no declared count → `mid`** (never assume)
* **`strong` is never derived.** A local route reaches `strong` only through an
  explicit `capability_tier` in `model-catalog-overrides.json`
  (`model_catalog.py:130`, applied at `model_catalog.py:203`).

**Why `strong` is not derived from parameter count.** On the 16 GB reference card the
only local models a `≥24B → strong` rule would promote are the ones that do not fit:
`gemma3:27b` is ~17 GB. From a `mid` 9B, the escalate rung admits only `strong`
(`minimum_rank_exclusive`, `model_assignment.py:170`), so a count-derived `strong`
would make the rung's designed target the OOM model. Parameter count is an *ordering*
signal; VRAM fit is a *deployment* fact, and only the operator has it. Gating `strong`
on an explicit override puts the decision where the fact is.

**4b. A single-model deployment must never become unassignable.** `model_selector.py:71`
(`if effective_rank < requested_rank: continue`) is a **hard exclusion**, and task
difficulty defaults to `mid` in `model_assignment.py:84`, `ledger.py:416`,
`ledger.py:705` and `schemas.py:84`. Without 4a, a lone `local.qwen2.5-coder:7b` sits
at `mid` and is selected; with a naive 4a it becomes `light`, every default-difficulty
task fails to match, and `select` returns `NoCapableModel("no_capable_model")`
(`model_selector.py:85-86`). Three guards:

* The `≤8B → light` rule is why 4a's boundary is 8B and not higher — but that alone is
  not a guard, so:
* **Empty-result fallback on initial assignment.** In the initial-assignment path
  (`model_assignment.py:106`), when `select` returns `NoCapableModel`, retry `select`
  at the highest tier the available pool can actually satisfy and record a
  `difficulty_downgraded` decision naming the requested tier, the satisfied tier and
  the route. A deployment is never left unable to start work; it is left with a
  *recorded* statement that it is running below the requested tier. The escalation
  path (`model_assignment.py:165`) does **not** get this fallback — escalation is
  supposed to be able to find nothing, and I5 below makes that visible.
* **Mandatory test.** A one-route local pool at default `mid` difficulty assigns that
  route and records the downgrade. This is a DoD gate, not a nice-to-have.

**4c. Known asymmetry, stated not fixed.** `_effective_rank` (`model_selector.py:43-44`)
**demotes** a route by one rank on a poor corpus record and nothing ever promotes one.
Under 4a a `light` local route that is demoted floors at rank 0 and can then be
excluded from `mid` work permanently, with no path back. This spec does not add
promotion (that is a corpus-semantics change with its own evidence bar); it names
`model-catalog-overrides.json` as the operator's reset and requires the downgrade
decision from 4b to make the state legible.

**4d. Known limits of parameter-count parsing (M1).** `_PARAM_BILLIONS_RE`
(`model_catalog.py:67`) requires a separator before the digits and a `b` word boundary
after. On real ollama ids that yields:

| Route | Parsed | Tier under 4a | Reality |
|---|---|---|---|
| `local.mistral-small3.1:latest` | `None` | `mid` | 24B / ~15 GB — an explicit `:24b` spelling would be the same `mid`, which is at least consistent under 4a, but the size is invisible |
| `local.mixtral:8x7b` | `None` | `mid` | 47B total |
| `local.llama4:16x17b` | `None` | `mid` | 108B total |
| `local.qwen3-coder:30b-a3b` | `30.0` | `mid` | 3B *active* — a MoE's total is not its strength |

These are limits of a name-parsing heuristic and are not fixable by a better regex; a
model id is not a spec sheet. Under 4a they all land on the safe default (`mid`), which
is the correct failure direction. **`model-catalog-overrides.json` is the remedy**, and
per issue #82 it should be documented as *required configuration* for a local-only
team, not as a debugging tool. This spec adds that to the local-model operator docs.

**4e. Correcting an earlier claim (I4).** An earlier draft said "an explicit
`metadata.model_tier` override still wins". It does not. `member_tier`
(`model_tier.py:61-70`) reads `metadata.model_tier`, but the selector never calls it —
`select` reads `ModelCatalogEntry.capability_tier` (`model_selector.py:38`), which
comes from `default_entry`/`tier_for_route` and is overridable **only** through
`model-catalog-overrides.json` (`model_catalog.py:196`, `:203`). `metadata.model_tier`
governs the F127 role-tier comparisons, not model selection.

**4f. Scope of the gateway knobs (M2).** Moves 2 and 3 apply to `_ollama_dispatch`
only. A local model served over an OpenAI-compatible endpoint reaches the box through
`_registry_dispatch` (`gateway_local.py:253-270`) under the `custom` provider class
(diverted at `gateway_local.py:234-242`) and receives neither `think` nor `format`.
That is a real gap for `custom`-routed local models and is out of scope here; the
`local_*` knob names and the operator docs must say "`local.*` routes via Ollama",
not "local models".

**4g. An unavailable rung is recorded with its reason (I5).** When
`next_escalation_assignment` yields no rung, `autonomy` records a decision naming the
rung *and a discriminated reason* — never a flattened `None`:

| Reason | Site | Meaning |
|---|---|---|
| `no_current_assignment` | `model_assignment.py:155-156` | the task never had a model assignment to escalate from |
| `empty_pool_snapshot` | `model_assignment.py:159-160` | no `model_pool_snapshot` on the task |
| `unavailable` | `model_selector.py:85` via `model_assignment.py:172-173` | candidates exist in the pool but none is currently reachable |
| `no_capable_model` | `model_selector.py:85` via `model_assignment.py:172-173` | candidates are reachable but none outranks the current route — **the #82 case** |

`next_escalation_assignment` returns the reason alongside the (absent) assignment so
`autonomy` can record it; today it collapses all four to `None`
(`model_assignment.py:156`, `:160`, `:173`).

A fifth condition is **not** an unavailable rung and must not be recorded as one:
`autonomy.py:3192-3196` skips the escalation call entirely once
`current_escalations >= policy.model_escalation_limit`. That rung was *walked* to
exhaustion; it already has its own accounting (`c.model_escalations`, surfaced at
`autonomy.py:2944-2948`) and is recorded as `escalation_budget_exhausted`, distinct
from the four above.

## Risks

**Move 4 changes model selection on every existing local deployment.** Today all local
routes tie at `mid` (`model_tier.py:42`); afterwards a `7b` becomes `light` and may
stop being selected for mid-difficulty work it currently receives. It sits behind
`ERRORTA_LOCAL_SIZE_TIERS` (default off), the 4b fallback keeps a single-model box
working, and the tie-breaking is asserted explicitly in tests.

**Move 3 can degrade content quality.** #84's own caveat: constrained decoding fixes
shape and may cost substance. This spec does not settle that — it makes the knob exist
and couples it safely. The A/B on verdict *usefulness* needs a real council run.

**Move 2 assumes `think:false` does not hurt reasoning-heavy turns.** Measured only on
a reviewer verdict — a shape-heavy, reasoning-light turn — on one model. A DEV
implementation turn may genuinely want the reasoning, and DEV turns are among the six
tagged sites (`runner.py:5433`, `:5458`). Mitigation: the tag is applied per call site,
so the DEV sites can opt out independently of the governance/review sites; whether they
should is a measurement this spec does not have.

**Move 1 changes what a truncated turn costs.** Today a truncation is fast and wrong.
Afterwards it is a failed parse, up to the corrective-retry limit, then an unproductive
turn — slower and correct. On a ~100 s local turn that is real wall-clock. SPEC-42
landing first is what keeps this from being the common path.

## Escape hatches

| Knob | Where | Default | Disable value restores |
|---|---|---|---|
| `local_truncation_guard` | `CodingAutonomyPolicy` → `build_run_turn` → request metadata | `True` | today's `THINKING_TRACE_MARKER` substitution on any empty content; no `truncated` flag |
| `local_think_false` | `CodingAutonomyPolicy` → `build_run_turn` → request metadata | **`False` until the second-model reproduction lands** | thinking left on for structured turns |
| `local_structured_format` | same path; additionally gated on `local_think_false` | `False` | no `format` sent |
| `ERRORTA_LOCAL_SIZE_TIERS` | env var read in `model_catalog` (no policy is in scope at `tier_for_route`) | unset (off) | every `local.*` route at `MID` |
| `model-catalog-overrides.json` | `model_catalog.py:130` | absent | per-route `capability_tier`/`cost_tier`/`size_rank`/`speed_rank`; the operator's override for 4c/4d and the required config for local-only teams |

Each disable value reproduces today's trace exactly, and that is asserted per knob.

## Definition of done

**Sequencing**

- SPEC-42 has landed and the coding turn's output budget clears the reasoning preamble
  on the reference box; this spec's moves are measured above that budget, not below it.

**Move 1 — truncation honesty**

- A truncated turn (`done_reason == "length"`) never reaches a caller as
  marker-prefixed thinking text presented as an answer.
- A genuine thinking-only response (`done_reason == "stop"`, empty content) still gets
  the marker, unchanged.
- A truncated turn produces **no** `MEMBER_UNHEALTHY` stop: a test drives three
  consecutive deterministic truncations and asserts the run reaches the F127
  unproductive ladder (`autonomy.py:3150`), not `autonomy.py:3850`.
- `truncated` is readable from the persisted turn record on the coding path (via the
  `_usage_sink` key) without any change to `MemberCaller` (`runner.py:86`).
- `scheduler.py:3954` rejects a truncated result and a blank-content result as the
  answer of record; `scheduler.py:2780` and `:2974` reject a truncated result.

**Moves 2 and 3 — thinking and format**

- The six coding call sites (`runner.py:5433/5458/5508/5526/5635/5654`) and the eight
  Council-room sites tag `structured_output`; a test asserts the gateway receives it
  and that `gateway_member_caller`'s ten call sites are unchanged.
- `gateway_local` imports nothing from `errorta_council.coding`, asserted by a new case
  in `tests/council/test_import_lint.py`.
- `format` is **never** sent on a thinking-capable route with thinking left on — a test
  asserts the combination is unreachable through the knobs, not merely discouraged.
- `local_think_false` defaults on **only after** the 6/6 schema result is reproduced on
  a second thinking-capable model (`deepseek-r1` or `qwq`) on the reviewer-verdict
  shape. Until then it defaults `False` and this bullet is the gate.
- ~~The `think`-on-a-non-thinking-model probe (Move 2's open item) has been run on the
  reference box and its outcome is recorded in the implementation PR.~~ **DONE
  2026-08-06** — `qwen2.5-coder:7b` and `gemma3:27b` both return HTTP 200 with correct
  content when sent `think:false`. The field is unconditional; the marker-table
  fallback is dropped. See Move 2.

**Move 4 — tiers and rungs**

- **Single-model deployment test (mandatory).** A pool of exactly one
  `local.qwen2.5-coder:7b`, with `ERRORTA_LOCAL_SIZE_TIERS` on and a default-`mid`
  task, still assigns that route and records a `difficulty_downgraded` decision. No
  `NoCapableModel` escapes to the caller.
- On a pool of local routes of differing sizes, `tier_for_route` yields more than one
  distinct rank, and an exhausted task can escalate from a `light` route to a `mid`
  one.
- No `local.*` route reaches `strong` without an explicit
  `model-catalog-overrides.json` entry — asserted for `gemma3:27b` specifically, the
  model that does not fit the reference card.
- With a single local route, the escalate rung records `no_capable_model`, and each of
  the other three reasons is asserted by its own case; `escalation_budget_exhausted` is
  recorded as a walked rung and never as an unavailable one.
- The M1 parsing limits are covered by a table-driven test over
  `mistral-small3.1:latest`, `mixtral:8x7b`, `llama4:16x17b`, `qwen3-coder:30b-a3b`,
  asserting the documented (not the intuitive) answer, so a future change to the regex
  cannot silently reclassify them.
- The operator docs name `model-catalog-overrides.json` as required configuration for a
  local-only team.

## Out of scope

- **A VRAM-fit check.** `param_billions` gives an ordering, not a fit decision; nothing
  in the catalog knows the card. Move 4a's refusal to derive `strong` is a *mitigation*
  of that gap, not a fix; escalating to a model that will not fit remains a real risk
  on a 16 GB box and belongs with model availability.
- **Local models served over the `custom` provider.** They dispatch through
  `_registry_dispatch` (`gateway_local.py:253`) and get neither knob (4f).
- **Promotion in `_effective_rank`.** The demote-only asymmetry (`model_selector.py:43-44`)
  is named in 4c and left alone.
- **Raising the coding turn's output budget.** SPEC-42 owns it.
- **Re-evaluating F001's judge choice.** This spec supplies the evidence (`qwen3.5:9b`
  + `think:false` is 6/6 on the reviewer verdict on one model and one prompt shape, so
  the 15 GB `mistral-small3.1` co-residency buys nothing *on this evidence*); the
  product decision is F001's, and the n=6 caveat travels with the number.
- **Verdict-usefulness A/B.** Needs a real council run scored on merge-gate pass rate,
  which the benchmark explicitly names as the highest-value follow-up.
