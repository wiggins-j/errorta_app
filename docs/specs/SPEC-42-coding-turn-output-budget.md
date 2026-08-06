# Spec 42 — The coding council's per-turn output budget must fit the model

**Source:** §4.1a of the RX 9060 XT benchmark
(`docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md:277-337`), whose adversarial-review
correction found that the coding council is truncating reasoning-model turns *today* —
plus a code audit of the coding turn seam on `fix/local-model-integration`.
**Target version:** v0.1 (engine — `coding/runner.py`, `coding/wizard.py`,
`coding/autonomy.py`, `routes/coding.py`, a new `errorta_council/reasoning_budget.py`)
**Depends on:** nothing — this is the *prerequisite*.
**Blocks:** [SPEC-41](SPEC-41-local-model-integration.md) (all four moves; see
"Why this must land first")
**Status:** LANDED (verified 2026-08-06 against the code, not the commit log)
**Landed evidence:** reasoning_budget.py leaf; consumed runner.py:8155 with the local-vendor gate :8156; interactive floor wizard.py:238 + routes/coding.py:62
**Tests:** tests/coding/test_spec42_turn_output_budget.py (14)

> **Line numbers in this spec are as of `98a745b`** on `fix/local-model-integration`.
> An earlier revision cited `coding/runner.py` at pre-`98a745b` offsets; every
> `runner.py` citation below has been re-verified against the current tree (the old
> ones were uniformly 34 lines low).

---

## Problem

The mitigation for reasoning-model thinking-burn exists, and the coding council cannot
see it.

**The mitigation.** `scheduler.py:1725-1760` resolves a per-turn output budget from the
member's model: `DEFAULT_MAX_OUTPUT_TOKENS = 2048` (scheduler.py:1732),
`REASONING_MAX_OUTPUT_TOKENS = 8192` (scheduler.py:1733), a
`REASONING_TIMEOUT_FLOOR_SECONDS = 300` wall-clock floor (scheduler.py:1738), and
`_is_reasoning_model` (scheduler.py:149) matching a marker list that includes `"qwen3"`
(scheduler.py:143-146). Its own comment (scheduler.py:1723-1731) names the failure mode
exactly: a low budget "makes them emit a thinking-burn with no answer." Note the three
constants are **class attributes**, read as `self.DEFAULT_MAX_OUTPUT_TOKENS`
(scheduler.py:1740-1751), not module globals — Move 1 must keep them bound on the class.

**The coding council does not dispatch through it.** Every coding turn — PM, DEV,
REVIEWER, TESTER — reaches the gateway through `coding/runner.py:7617`
`gateway_member_caller`, which builds its own request:

```python
max_output_tokens=int(tl.get("max_output_tokens", 2048) or 2048),   # runner.py:7656
timeout_seconds=int(tl.get("timeout_seconds", 600) or 600),        # runner.py:7664
```

It never calls `_is_reasoning_model` and never reads `REASONING_MAX_OUTPUT_TOKENS`.
Grepped over the tree: `_is_reasoning_model`, `DEFAULT_MAX_OUTPUT_TOKENS`,
`REASONING_MAX_OUTPUT_TOKENS` and `REASONING_TIMEOUT_FLOOR_SECONDS` appear in **no
`.py` file outside `scheduler.py`** (the only other hits are prose in
`docs/coding/model-eval/*.py` headers).

`max_output_tokens` is not advisory: `gateway_local.py:346` puts it straight into the
Ollama request as `options.num_predict`.

**The measurement.** `senditai` (Ubuntu + ROCm, RX 9060 XT 16 GB), `qwen3.5:9b`, six
cache-busted trials per arm, on `/api/chat` — the route the council actually uses
(`gateway_local.py:340`):

| Arm | schema ok | `done_reason` | mean `eval_count` |
|---|---|---|---|
| `num_predict=800`, thinking **on** | 1/6 | `length` ×5, `stop` ×1 | 727 |
| `num_predict=8192`, thinking **on** | 4/6 | `stop` ×6 | **2197** |
| `num_predict=800`, `think:false` | 6/6 | `stop` ×6 | 33 |

**2197 > 2048.** The coding council sends a budget smaller than the mean generation
length of a reasoning model it will happily seat (`local.qwen3.5:9b` is a legal route;
`ctask4/run_config.json` on the reference box is an all-local team on
`local.qwen2.5-coder:7b`).

**Precisely when reviewer verdicts start being clipped.** An earlier revision said
"reviewer verdicts on such a team are being clipped now." That was an inference, and it
was premature in both directions:

* The 2197 mean was measured by standalone scripts hitting `/api/chat` directly
  (`docs/coding/model-eval/budget_vs_think.py`, `decisive_budget_test.py`), **not**
  through `gateway_member_caller`. It is a property of the model, not an observation of
  the coding path.
* Before `98a745b`, an all-local PM or REVIEWER turn never reached a truncation at all:
  the member carried no `model`, so the request went out with `model=""` and Ollama
  answered `HTTP 400 {"error":"model is required"}` before generating a token (see the
  FIXED-SEPARATELY note in Out of scope).

**So the honest framing is: as of `98a745b`, those turns now reach the model and will
truncate.** The defect this spec fixes is not newly introduced by that commit — the 2048
literal predates it — but `98a745b` is the commit that makes it *observable* rather than
masked by a hard failure upstream. That is a stronger claim than the one it replaces,
because it is dated and checkable.

**And the clipping is disguised as an answer.** When `content` comes back empty,
`gateway_local.py:390-393` substitutes `THINKING_TRACE_MARKER + thinking`. A truncation
therefore arrives at the council as a *reasoning trace presented as an answer* — which
the parser then reads as a malformed model, not as a budget we set too low.

### Three corrections to the framing this spec was commissioned with

Each was checked before designing around it.

1. **There is no persisted `2048` to migrate.** The claim that existing teams carry
   `turn_limits.max_output_tokens: 2048` from `coding/wizard.py:222` does not hold.
   That dict belongs to `_synthetic_member` (wizard.py:216-223), the **Wizard's own
   conversational PM**, constructed fresh per call and never written to a team.
   `coding/recipes.py:64-78` `resolve_team` — the real create-on-accept team builder
   (`routes/coding.py:2052`) — writes **no `turn_limits` at all**. Inspecting all
   eleven `run_config.json` files under `~/.errorta/council/coding-projects/`: nine
   members have `turn_limits` absent or `{}`; the only populated ones are `8192`/`6144`
   on `pixel-creatures` and the Godot RPG, which come from the shared editor's presets
   (`src/features/rooms/CouncilRoomEditor.tsx:958-1029`) and are deliberate. **The 2048
   is not data; it is the literal at runner.py:7656.** A code-only fix does reach every
   existing team.

   Two properties of those two populated configs matter later (verified by inspection):
   they set **only** `max_output_tokens` — `8192` on `m-pm`/`m-dev-*`, `6144` on
   `m-review-*`/`m-test-*` — and carry **no `timeout_seconds` key at all**. Any escape
   hatch that *replaces* `turn_limits` would therefore cut a real operator budget from
   8192 to 2048. See "Escape hatches".

2. **The 120 s timeout is not on the loop path.** Persisted coding members carry no
   `turn_limits`, so `runner.py:7664` gives loop turns **600 s**, already above the
   scheduler's 300 s reasoning floor. The 120 s cliff is real but confined to three
   *interactive* paths: `wizard.py:222` (`timeout_seconds: 120`), pm-ask
   (`routes/coding.py:1881`, `min(configured, 120)` over a **90 s** default read at
   :1878) and the directive interpreter `_pm_complete` (`routes/coding.py:2171`, same
   clamp over a 120 s default at :2168).

3. **A policy knob cannot reach `gateway_member_caller` — but it can reach the loop.**
   The seam takes `(gateway)` and returns `(member, prompt) -> str`. It is defined at
   runner.py:7617 and has **nine production call sites** — wizard.py:257;
   routes/coding.py:1885, 2173, 2660, 2806, 4436; `scripts/validate_f127_live.py:80`,
   `validate_f087_live.py:139`, `demo_calculator_live.py:85` — and no policy argument.
   (An earlier revision said "ten call sites"; that count included the definition.)
   The precedent for plumbing a policy field into turns already exists and is not that
   seam: `dev_repo_read` is a `CodingAutonomyPolicy` field (autonomy.py:228) passed as a
   keyword to `build_run_turn` (runner.py:5331, parameter at :5339) by
   `CodingRunner.run` (runner.py:7863). §"Escape hatches" uses it.

### Why this must land first (the SPEC-41 interlock)

An earlier revision of this section argued urgency from a SPEC-41 draft that no longer
exists: it claimed "SPEC-41 Move 2 raises `RetryableError` on `done_reason == length`,
which member-health turns into a 3-strike halt." Two things are wrong with that.
Truncation honesty is SPEC-41 **Move 1** (SPEC-41:194), not Move 2 (`think:false`,
SPEC-41:262); and SPEC-41 rev.2 **explicitly rejects raising** — "**Do not raise.**"
(SPEC-41:206), with SPEC-41:208-232 walking the same member-health chain
(`member_health.py:177` → `classify_aware_cap` at `:196-208` →
`policy.member_failure_limit = 3`, autonomy.py:141) to explain why. The argument is
sound; SPEC-41 already made it, and made it against itself.

**The real ordering argument is the one SPEC-41 states at :120-123**, and it is a
measurement argument, not an outage argument:

> Until `runner.py` stops hardcoding 2048, a thinking-capable local model truncates on
> every structured turn and moves 1–3 are **measured through a defect SPEC-41 does not
> own**.

Concretely: SPEC-41 Move 1 makes truncation *legible* (`truncated: bool`, marker
suppressed). Landing it against today's budget produces a stream of correctly-labelled
truncations that are entirely our own doing — true reports of a self-inflicted
condition, which tell us nothing about the model. Moves 2-3 (`think:false`,
`format:"json"`) are then evaluated on turns that were already clipped, so any schema
result they produce is uninterpretable. Budget first, so SPEC-41's moves are measured
against a model that was allowed to finish.

This is the same argument as row 2 of "Validation sequencing — do not reorder" below.

## Principle

> A turn's output budget is a property of the model taking the turn, resolved once, in
> one place, by every path that dispatches a turn. A number that only one scheduler
> knows is not a policy — it is a coincidence.

## What this spec does

### Move 1 — one leaf module owns the budget rule

New `python/errorta_council/reasoning_budget.py`, stdlib-only, exporting:

* `is_reasoning_model(model: str) -> bool` — the marker list moved verbatim from
  `scheduler.py:143-151`.
* `DEFAULT_MAX_OUTPUT_TOKENS = 2048`, `REASONING_MAX_OUTPUT_TOKENS = 8192`,
  `REASONING_TIMEOUT_FLOOR_SECONDS = 300` — moved from `scheduler.py:1732-1738`. They
  are class attributes there (`self.DEFAULT_MAX_OUTPUT_TOKENS`, scheduler.py:1744-1751),
  so the class keeps assignments bound to the imported module constants; the reads at
  scheduler.py:1740-1755 are untouched.
* `resolve_turn_budget(model_id: str, *, default_max_output_tokens,
  default_timeout_seconds, explicit: dict) -> (max_output_tokens, timeout_seconds)` —
  the single resolver. It **takes the already-resolved model id**; it does not derive
  one (see Move 2). **An explicit `turn_limits` value always wins**, preserving
  `_base_output_tokens_for`'s contract (scheduler.py:1740-1746).

`scheduler.py` imports from it and keeps `_is_reasoning_model` as a module-level alias
(the eval scripts' headers name it; `docs/coding/model-eval/decisive_budget_test.py:5`).
Behaviour there is unchanged.

*Why a new leaf and not the alternatives:*

* **Not `coding/runner.py` importing `scheduler`.** No hard import cycle exists today
  (`scheduler`'s only coding import is lazy, inside a function at scheduler.py:3402),
  so it would work. The reason not to is **import weight**, plainly: it drags
  `callouts`, `steward`, `context.dialect` and `topologies` (scheduler.py:19-36) into
  every coding process for two integers and a substring test. An earlier revision also
  called it a layering inversion; there is no enforced layering rule in this repo that
  says so, and the honest objection is the weight.
* **Not `limits.py`.** It is a genuine leaf and already imported by the scheduler, but
  its documented invariant is "caps are frozen at run creation" (limits.py:1-6). A
  model-derived default is not that.
* **Not "seed a bigger number in `wizard.py:222`."** Per correction 1, that dict never
  reaches a team. It would fix the Wizard's own chat and nothing in the run loop.

**`REASONING_TIMEOUT_FLOOR_SECONDS` moves but is not consumed by the coding path.** On
the loop the default is already 600 s, so `max(600, 300) = 600` — the floor never binds
(Move 3's table). It moves anyway so the three constants stay one unit rather than
being split across two modules, and so the interactive floor of Move 4 has an obvious
sibling to sit next to. If review prefers, leaving it in `scheduler.py` costs this spec
nothing.

### Move 2 — take the model id the turn actually carries; do not re-derive it

**This move already shipped, in `98a745b`, and this spec must not re-propose it.**
`runner._member_model_id` (runner.py:110-141) prefers `model`/`model_display` and falls
back to the `gateway_route_id` suffix (`local.qwen3.5:9b` → `qwen3.5:9b`) — the same
derivation `bind_member_route` performs (model_assignment.py:55). It is already wired at
runner.py:7654 and covered by `tests/coding/test_unbound_member_model_id.py`.

The reason the fallback is needed is unchanged and still governs this spec's design:
only **worker** turns get a route-bound copy — `bind_member_route`
(model_assignment.py:51-66) has one call site, runner.py:6392, on the `Assign` path. PM
governance turns (runner.py:5542) and governance review turns (runner.py:5669) pass the
raw member, and a persisted member carries only `gateway_route_id`, `id`, `metadata`,
`model_mode` (verified: `ctask4/run_config.json`). So `member.get("model")` alone is
empty **on exactly the turns the benchmark measured** (the reviewer verdict).

**Consequence for Move 1:** `resolve_turn_budget` takes `model_id` as a parameter. The
seam supplies it as `_member_model_id(member)` — the value it *already computes* one
line above the budget line (runner.py:7654 vs :7656). A second derivation inside the
budget module would be a duplicate that can silently disagree with the id actually sent.

### Move 3 — the seam resolves its own defaults, on local routes only

`gateway_member_caller` (runner.py:7656/7664) stops hardcoding. When `turn_limits`
carries no explicit value it calls `resolve_turn_budget`:

| route vendor | model | `max_output_tokens` | `timeout_seconds` |
|---|---|---|---|
| `local` | reasoning marker matched | 8192 | `max(600, 300)` = 600, unchanged |
| `local` | everything else | 2048, unchanged | 600, unchanged |
| anything else | any | 2048, unchanged | 600, unchanged |

**The vendor gate is mandatory, not a refinement.** `gateway_member_caller` is
provider-agnostic, and the marker list matches real *hosted* route ids: `openai.o1`,
`openai.o3`, `openai.o3-mini` (via `"o1"`/`"o3"`),
`google.gemini-2.0-flash-thinking-*` and `anthropic.*-thinking` (via `"thinking"`).
Those providers treat `max_output_tokens` as a hard billed cap —
`async_anthropic.py:104` (`"max_tokens": request.max_output_tokens or 1024`),
`async_openai.py:63-69`, `async_google.py:93-94` — so an ungated raise would quadruple
the ceiling on paid routes, directly contradicting this spec's own "Out of scope:
raising the default for hosted API providers … paid tokens". Worse, it would not even
work where it fires: `async_openai.py:64-68` documents that o-series models require
`max_completion_tokens`, and the provider sends `max_tokens`, so on `openai.o1`/`o3`
the raise is billed-risk with no effect.

The gate is `_member_vendor(member) == "local"` — the helper already in the module at
runner.py:99, deriving the `gateway_route_id` prefix with a `provider_kind` fallback.
Reusing it (rather than importing `model_catalog.provider_class`, model_catalog.py:38)
keeps the seam's lazy-import discipline intact.

This is deliberately policy-free, so it applies at all nine call sites without a
plumbing story, and it is a **default**, so any member that states a value keeps it.

### Move 4 — the interactive paths get a reasoning floor

**Correcting an error in the previous revision.** It proposed changing
`routes/coding.py`'s `min(configured, 120)` clamp to `min(configured, 240)`. That is a
literal no-op on both routes. pm-ask reads a **90 s** default (`routes/coding.py:1878`:
`configured = int(tl.get("timeout_seconds", 90) or 90)`) and `_pm_complete` reads 120
(`:2168`); persisted coding members carry no `turn_limits` at all (correction 1), so
`configured` *is* the default. `min()` cannot raise a value: `min(90, 240) = 90` and
`min(120, 240) = 120`. Nothing changes. (The previous revision also stated :1879's
default as 120; it is 90.)

The clamp is a ceiling, so a reasoning route needs a **floor**, applied to the resolved
value rather than to the operator's number:

* `wizard.py:222` (`_synthetic_member`) drops `max_output_tokens` entirely — inherit the
  resolved default — and its `timeout_seconds` becomes
  `INTERACTIVE_REASONING_TIMEOUT_SECONDS = 240` when `is_reasoning_model` matches the
  wizard's route, 120 otherwise. This is the only one of the three paths where the
  earlier revision's change would have had any effect, because it assigns directly.
* `routes/coding.py:1878-1881` (pm-ask) and `:2168-2171` (`_pm_complete`) keep their
  clamp for the non-reasoning case and take
  `max(min(configured, 240), 240)` — equivalently, `240` — on a reasoning route,
  i.e. the route's *default* rises to 240 while an explicitly-configured larger value is
  still clamped to 240 and an explicitly-configured smaller one is raised to it. Both
  paths must apply `is_reasoning_model(_member_model_id(m))`, not
  `m.get("model")`, for the reason in Move 2.

**Headroom, stated against the budget that will actually be in force.** 240 s is ~2.4×
the ~100 s verdict turn the benchmark measured — but that turn ran at the *old* budget
and generated ~2197 tokens, i.e. ~22 tok/s effective on this box. Against the new 8192
ceiling the worst case is ~370 s, so 240 s is roughly **0.65×** a full-ceiling turn: a
pathological interactive turn will time out. That is the intended trade — an interactive
chat that hangs for six minutes is a worse product than one that reports a timeout — but
it must be stated as a deliberate ceiling, not sold as headroom. The loop path is
unaffected: 600 s covers the ~370 s worst case with room. If SPEC-41 Move 2 lands,
`think:false` collapses these turns to ~33 tokens and the clamp stops mattering; this
spec must not depend on that.

### What is explicitly NOT done

**No migration and no "does this value look un-customised?" heuristic.** Per correction
1 there is nothing to migrate. A heuristic that treats a persisted `2048` as
un-customised would silently override an operator who typed 2048 deliberately in the
member editor (`CouncilRoomEditor.tsx:213-217`, whose help text at :58 explicitly tells
them to raise it for reasoning models) — trading a defect we can fix in code for a
defect that ignores a human. Explicit always wins.

## Risks

**The measurement is one model, one prompt shape, one contended box.** `qwen3.5:9b`,
a reviewer-verdict prompt, on `senditai` under load. 2197 is a mean over six trials; we
have no p99, so 8192 is not derived from a measured tail — it is chosen for **parity
with the scheduler's existing constant**, which is a consistency argument, not an
empirical one. A different model or a longer prompt could exceed it.

**A raised cap converts some truncations into long turns, and a few into timeouts.**
`num_predict` is a ceiling, not a reservation (gateway_local.py:346), so a model that
answers early pays nothing. But a pathological reasoning turn that used to stop at 2048
may now generate to 8192 — on this box roughly 6 minutes, which is inside the 600 s loop
timeout but outside the 240 s interactive clamp. That is a real regression *for that
turn*, and the correct one: a timeout is a legible failure, a truncated trace is not.

**`is_reasoning_model` is a substring heuristic, and it errs in BOTH directions.** It
matches `"qwen3"`, `"qwq"`, `"deepseek-r1"`, `"deepseek-reasoner"`, `"r1-"`, `"-r1"`,
`"thinking"`, `"reasoning"`, `"o1"`, `"o3"`, `"gpt-5-thinking"` anywhere in a model id
(scheduler.py:143-146). Moving the function does not improve it, and this spec does not
touch the list — but the blast radius grows, because a second dispatch path now consults
it.

* **False positives** (wasted ceiling, wasted VRAM window, no correctness cost): any
  `qwen3`-family model that does not think, notably `qwen3-coder:30b` / `:480b` — an
  instruct-only model and a plausible coding route — plus `qwen3-embedding` and
  `qwen3-reranker`.
* **False negatives — the larger half, and the one that leaves this bug live.** A
  reasoning model the list does not name keeps the 2048 budget and keeps truncating
  after this spec ships: `gpt-oss:20b`, `magistral`, `openthinker`, `smallthinker`,
  `exaone-deep`, `glm-z1`, `deepseek-v3.1`, `cogito`, `deepcoder`, `granite3.2`,
  `seed-oss`, `minimax-m1` locally, and `openai.o4-mini`, `gpt-5`, `gpt-5.1` on hosted
  routes (the latter three moot here anyway, since Move 3 gates on `local`).

  **Therefore this spec's Definition of Done is scoped to marker-matched routes.** It
  does not claim "reasoning models no longer truncate"; it claims "routes the marker
  list recognises no longer truncate." Closing the rest is the out-of-scope capability
  flag.

**Non-reasoning models are untouched, on purpose.** Item raised in review: raising
everything wastes latency and VRAM. The gate is the same marker test the scheduler
uses, so a `qwen2.5-coder:7b` team keeps 2048 exactly.

**A related, larger defect is deliberately left alone.** `max_output_tokens` is a hard
cap for hosted API providers too — `async_anthropic.py:104`, `async_openai.py:63-69`,
`async_google.py:93-94`. Today's 2048 default therefore also clips a long DEV diff on
an `anthropic.*` or `openai.*` coding route. Same defect class, but there is no
measurement behind a new number and the cost profile is different (paid tokens). This
is exactly why Move 3 gates on `local`. Named in Out of scope.

## Escape hatches

One knob on `CodingAutonomyPolicy`:

| Knob | Default | `False` means |
|---|---|---|
| `reasoning_output_budget` | `True` | **skip the model-derived default** on every loop turn — fall through to today's `tl.get("max_output_tokens", 2048)` / `tl.get("timeout_seconds", 600)` resolution, unchanged |

**`False` must never write literals into `turn_limits`.** A previous revision specified
that `False` stamps `{"max_output_tokens": 2048, "timeout_seconds": 600}` explicitly.
That is not a restore; on the two live teams that persist a budget it is a **4×
demotion**. `pixel-creatures` and the Godot RPG carry `max_output_tokens: 8192`
(`m-pm`, `m-dev-*`) and `6144` (`m-review-*`, `m-test-*`) and **no `timeout_seconds`
key** (verified, correction 1). Stamping the legacy pair would cut 8192 → 2048 on
exactly the deliberate operator budgets this spec elsewhere promises to protect, and
would invent a `timeout_seconds` those members never set. The hatch must **suppress a
default**, not **impose one**. If an implementation nonetheless stamps, it must MERGE
into the existing `turn_limits` and only for keys the member has not set — but
suppression is simpler and is the specified behaviour.

**How it reaches the turn, given the seam takes no policy.** Exactly the `dev_repo_read`
route (runner.py:5331/:5339 ← runner.py:7863): `CodingRunner.run` passes
`reasoning_output_budget=policy.reasoning_output_budget` into `build_run_turn`, which
applies it inside the shadowing `caller` at runner.py:5384 — the wrapper that already
sees every model exchange — by setting a suppression flag on the per-turn member copy
before delegating to `_raw_caller` (runner.py:5369). Move 3's "explicit always wins"
rule is untouched either way.

**Persistence is part of the knob, not an implementation detail.** A
`CodingAutonomyPolicy` field only round-trips through `autonomy.json` if it is added to
**both** `policy_to_dict` (autonomy.py:378-400 — `dev_repo_read` is the model, at :400)
and `policy_from_dict` (autonomy.py:448-498 — `dev_repo_read` at :498). Omit either and
the knob is unsettable and the hatch is decorative. Additionally,
`tests/coding/test_f145_pm_reference.py` asserts
`contract["autonomy_defaults"] == policy_to_dict(CodingAutonomyPolicy())`, so the new
field **must** also be added to the JSON block in `docs/coding/PM_REFERENCE.md` between
the `PM_REFERENCE_CONTRACT_START`/`END` markers, or that test fails hard.

**Where the knob does not reach, stated plainly.** The Wizard, pm-ask, the directive
interpreter, the delivery-review *route*, and the three `scripts/validate_*.py` entry
points call `gateway_member_caller` with no policy in scope. They get Moves 3-4
unconditionally.

Correcting the previous revision's reachability claim: the **runner-internal**
`delivery_review` (runner.py:7305, attached to `run_turn` at :7613) *is* covered — it
dispatches through `_parse_member_turn` (runner.py:7394), which uses the shadowed
`caller` from :5384. The genuine gap is `routes/coding.py:2814`, which builds its own
`build_run_turn(..., guardrail_enabled=...)` with **no policy keyword at all**, so it
takes the `reasoning_output_budget` default regardless of what the project's
`autonomy.json` says. Passing the policy there is a one-line follow-up, deliberately not
bundled here.

This is acceptable because what those paths receive is a *default*: an operator who
wants the old numbers sets `turn_limits` explicitly on the member (already supported,
already surfaced in the editor). Inventing a second, policy-free global switch to cover
them would be a config surface with no consumer.

## Definition of done

- `_is_reasoning_model` and the three constants live in one leaf module; `scheduler.py`
  imports them and its behaviour is unchanged (existing scheduler budget tests pass
  untouched).
- A PM/REVIEWER turn on a `local.qwen3.5:9b` team — the *unbound* member shape, no
  `model` key — resolves `num_predict = 8192`. This is the test that would have caught
  the bug: asserting it via a bound DEV member only would pass even if the seam read
  `member["model"]` directly.
- A member with an explicit `turn_limits.max_output_tokens` gets that value, reasoning
  model or not — including the persisted `8192`/`6144` shapes, which must be unchanged
  with the knob both `True` and `False`.
- A non-reasoning route (`local.qwen2.5-coder:7b`, `claude_cli.opus`) still gets 2048 /
  600 s.
- **A hosted route whose id matches a marker** (`openai.o3-mini`,
  `anthropic.claude-3-7-sonnet-thinking`, `google.gemini-2.0-flash-thinking-exp`) still
  gets 2048 — the `local` gate holds.
- `reasoning_output_budget=False` produces a `LocalCouncilModelRequest` field-identical
  to today's for every loop turn, on both an empty-`turn_limits` member and an
  `8192`-carrying member.
- The Wizard's PM turn no longer sends `max_output_tokens: 2048`, and its timeout is
  240 s on a reasoning route; pm-ask and `_pm_complete` resolve 240 s on a reasoning
  route and their existing defaults (90 s / 120 s) otherwise.
- Recorded in the spec's landing note: a live run on the reference box with an all-local
  reasoning team reaches a REVIEWER verdict through the **real coding path** (not
  `model-eval/` scripts), evidenced by what is observable *today*: non-empty `content`,
  **not** prefixed with `THINKING_TRACE_MARKER`, and a recorded output-token count
  above 2048. Asserting `done_reason == "stop"` is deliberately **not** in this spec's
  DoD — nothing reads `done_reason` yet; that read is SPEC-41 Move 1
  (SPEC-41:194-207), which is sequenced after this spec. The `done_reason == "stop"`
  assertion belongs in SPEC-41's DoD.

## Out of scope

- **SPEC-41's four moves.** `think:false`, `format: "json"`, the loud-truncation guard,
  and local size tiers all sit on top of this. None of them should land before this
  ships (see "Why this must land first" and SPEC-41:120-123).
- **Raising the default for hosted API providers.** Real, same defect class, no
  measurement, paid tokens — its own change. Move 3's `local` gate is what keeps this
  spec out of it.
- **Per-role or per-task-type budgets.** A DEV diff turn and a REVIEWER verdict plausibly
  want different budgets; nothing measured says what they are.
- **Improving the marker heuristic** (a capability flag on the route/catalog instead of
  substring matching on the model id) — moved verbatim here, worth doing, not here.
  This is what would close the false-negative list in Risks.
- **Passing the policy at `routes/coding.py:2814`.** The delivery-review route's
  `build_run_turn` call takes no policy keywords at all, so it defaults every policy
  field, not just this one. Fixing it is correct and independent.
- **A VRAM-fit check** for the larger generation window — belongs with model
  availability, as SPEC-41 already notes.
- ~~**The empty-`model` observation.**~~ **FIXED SEPARATELY, 2026-08-06 (`98a745b`).**
  On the non-worker turn paths (`runner.py:5542`, `:5669`) the member carried no
  `model`, so the seam sent `model=""` to `/api/chat` for a local PM/review turn.
  Verified against the reference box: Ollama answers
  `HTTP 400 {"error":"model is required"}`, which `_ollama_dispatch` maps to
  `FatalError("gateway_4xx: 400")` (the message lacks "not found", so it is not even
  reported as `model_not_found`). Fixed by `runner._member_model_id`
  (runner.py:110-141), which derives the id from the `gateway_route_id` suffix using
  `bind_member_route`'s own derivation (model_assignment.py:55); regression tests in
  `tests/coding/test_unbound_member_model_id.py`. This spec **consumes** that helper
  rather than re-deriving the id (Move 2).

## Validation sequencing — do not reorder

The local path has been evaluated through **two** plumbing defects, so any prior
"we ran it locally" evidence is uninterpretable. Both must land before the outstanding
model-quality questions can be answered:

| # | Defect | State |
|---|---|---|
| 1 | Unbound members send `model=""` → every PM-governance / governance-review turn of an all-local team hard-fails | **fixed** (`_member_model_id`, `98a745b`) |
| 2 | Coding turns send `num_predict=2048` vs a measured 2197-token mean → every reasoning turn truncates | **this spec** |

**Only after both:**

* ~~**Re-test F001's judge-schema failure.**~~ **DONE 2026-08-06 — it vanishes.**
  The pre-stated reading was: "if it vanishes, the judge recommendation is unfounded
  on the coding path too." It vanished. Harness:
  `docs/coding/model-eval/f001_judge_retest.py`, 8 trials/arm on `/api/chat`.

  | arm | qwen3.5:9b | mistral-small3.1 |
  |---|---|---|
  | A — 2048, thinking on (F001's conditions) | **2/8**, 6/8 truncated, 1678 tok | 8/8, 89 tok |
  | B — 8192, thinking on (this spec alone) | 7/8, 0 truncated, 1455 tok | 8/8, 74 tok |
  | C — 8192 + `think:false` + `format:"json"` | **8/8**, 8/8 direct-parse, 91 tok | 8/8, 172 tok |

  Attribution: **defect 2**. Arm A reproduces F001's failure and catches the
  mechanism in the act — 6 of 8 turns returned `done_reason: "length"`, i.e. the
  budget was consumed by the hidden trace before the answer began. Arm B recovers
  most of it on budget alone; arm C is clean. Defect 1 is not an arm here and could
  not have been: an empty model id is an HTTP 400 before the model is reached, so it
  cannot produce wrong-schema JSON.

  The control matters as much as the subject: `mistral-small3.1` scored 8/8 in
  *every* arm, because it has no thinking channel and never approached the cap. So
  F001's comparison was real but misattributed — it measured "immune to truncation",
  not "better at schemas". Post-fix the two tie, and the 9B model is cheaper.

  Note arm C's `direct_parse` column: 8/8 versus 0/8 for mistral in arms A/B. That
  is `format:"json"` removing the fence-wrapping every structured turn was
  previously surviving on extraction heuristics.
* **The benchmark's own highest-value follow-up** — a real council run scored on
  merge-gate pass rate rather than isolated function correctness — can share that
  harness. It is also the only thing that can answer whether `think:false` and
  `format:"json"` cost verdict *usefulness* rather than merely fixing shape.

Running either earlier produces a number that cannot be attributed.
