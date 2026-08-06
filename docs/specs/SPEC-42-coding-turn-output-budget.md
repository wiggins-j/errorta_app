# Spec 42 — The coding council's per-turn output budget must fit the model

**Source:** §4.1a of the RX 9060 XT benchmark
(`docs/coding/LOCAL_MODEL_SELECTION_RX9060XT.md:277-337`), whose adversarial-review
correction found that the coding council is truncating reasoning-model turns *today* —
plus a code audit of the coding turn seam on `fix/local-model-integration`.
**Target version:** v0.1 (engine — `coding/runner.py`, `coding/wizard.py`,
`coding/autonomy.py`, `routes/coding.py`, a new `errorta_council/reasoning_budget.py`)
**Depends on:** nothing — this is the *prerequisite*.
**Blocks:** [SPEC-41](SPEC-41-local-model-integration.md) (Move 2 in particular; see
"Why this must land first")
**Status:** proposed · **Owner:** wiggins-j

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
exactly: a low budget "makes them emit a thinking-burn with no answer."

**The coding council does not dispatch through it.** Every coding turn — PM, DEV,
REVIEWER, TESTER — reaches the gateway through `coding/runner.py:7583`
`gateway_member_caller`, which builds its own request:

```python
max_output_tokens=int(tl.get("max_output_tokens", 2048) or 2048),   # runner.py:7622
timeout_seconds=int(tl.get("timeout_seconds", 600) or 600),        # runner.py:7630
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
`local.qwen2.5-coder:7b`). Reviewer verdicts on such a team are being clipped now.

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
   is not data; it is the literal at runner.py:7622.** A code-only fix does reach every
   existing team.

2. **The 120 s timeout is not on the loop path.** Persisted coding members carry no
   `turn_limits`, so `runner.py:7630` gives loop turns **600 s**, already above the
   scheduler's 300 s reasoning floor. The 120 s cliff is real but confined to three
   *interactive* paths: `wizard.py:222` (`timeout_seconds: 120`), pm-ask
   (`routes/coding.py:1879`, `min(configured, 120)`) and the directive interpreter
   `_pm_complete` (`routes/coding.py:2170`, same clamp). Against a ~100 s thinking turn
   those give ~1.2× headroom on a contended GPU.

3. **A policy knob cannot reach `gateway_member_caller` — but it can reach the loop.**
   The seam takes `(gateway)` and returns `(member, prompt) -> str`, with ten call
   sites (runner.py:7583 definition; wizard.py:254-257; routes/coding.py:1885, 2173,
   2660, 2806, 4436; `scripts/validate_f127_live.py:80`, `validate_f087_live.py:139`,
   `demo_calculator_live.py:85`) and no policy argument. The precedent for plumbing a
   policy field into turns already exists and is not that seam: `dev_repo_read` is a
   `CodingAutonomyPolicy` field passed as a keyword to `build_run_turn`
   (runner.py:5305) by `CodingRunner.run` (runner.py:7825). §"Escape hatches" uses it.

### Why this must land first (the SPEC-41 interlock)

SPEC-41 Move 2 makes a truncation **fatal**: on `done_reason == "length"` it suppresses
the marker substitution and raises `RetryableError("local_output_truncated")`. Today,
with a 2048 budget against a mean 2197, *every* thinking-on turn on the reference model
truncates. `classify_member_failure` (member_health.py:177-193) maps an unrecognised
message to `errored`, which `classify_aware_cap` (member_health.py:196-208) caps at
`policy.member_failure_limit = 3` (autonomy.py:141). Landing Move 2 against today's
budget converts a council that degrades into a council that **stops after three turns**
with a blocking member-health Problem.

Honesty about a truncation is right. Honesty about a truncation *we are causing on
purpose* is a self-inflicted outage. Budget first.

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
  `REASONING_TIMEOUT_FLOOR_SECONDS = 300` — moved from `scheduler.py:1732-1738`.
* `model_id_of(member: dict) -> str` — see Move 2.
* `resolve_turn_budget(member, *, default_timeout_seconds) -> (max_output_tokens,
  timeout_seconds)` — the single resolver. **An explicit `turn_limits` value always
  wins**, preserving `_base_output_tokens_for`'s contract (scheduler.py:1740-1746).

`scheduler.py` imports from it and keeps `_is_reasoning_model` as a module-level alias
(the eval scripts' headers name it; `docs/coding/model-eval/decisive_budget_test.py:5`).
Behaviour there is unchanged.

*Why a new leaf and not the alternatives:*

* **Not `coding/runner.py` importing `scheduler`.** No hard import cycle exists today
  (`scheduler`'s only coding import is lazy, inside a function at scheduler.py:3401),
  so it would work — and it is still wrong. It inverts layering (the coding engine
  taking a dependency on the *deliberation* engine) and drags `callouts`, `steward`,
  `context.dialect` and `topologies` (scheduler.py:19-36) into every coding process for
  two integers and a substring test.
* **Not `limits.py`.** It is a genuine leaf and already imported by the scheduler, but
  its documented invariant is "caps are frozen at run creation" (limits.py:1-6). A
  model-derived default is not that.
* **Not "seed a bigger number in `wizard.py:222`."** Per correction 1, that dict never
  reaches a team. It would fix the Wizard's own chat and nothing in the run loop.

### Move 2 — resolve the model id the way the turn actually carries it

`resolve_turn_budget` must not read `member["model"]` alone. Only **worker** turns get
a route-bound copy: `bind_member_route` (model_assignment.py:51-66) is called once, at
runner.py:6358, on the `Assign` path. PM governance turns (runner.py:5508) and review
turns (runner.py:5635) pass the raw member, and a persisted member carries only
`gateway_route_id`, `id`, `metadata`, `model_mode` (verified:
`ctask4/run_config.json`). So `member.get("model") or member.get("model_display")` —
what runner.py:7620 reads — is **empty on exactly the turns the benchmark measured**
(the reviewer verdict).

`model_id_of` therefore falls back to the `gateway_route_id` suffix
(`local.qwen3.5:9b` → `qwen3.5:9b`), which is the same derivation
`bind_member_route` performs (model_assignment.py:54).

### Move 3 — the seam resolves its own defaults

`gateway_member_caller` (runner.py:7622/7630) stops hardcoding. When `turn_limits`
carries no explicit value it calls `resolve_turn_budget`:

| model | `max_output_tokens` | `timeout_seconds` |
|---|---|---|
| reasoning marker matched | 8192 | `max(600, 300)` = 600, unchanged |
| everything else | 2048, unchanged | 600, unchanged |

This is deliberately policy-free, so it applies at all ten call sites without a
plumbing story, and it is a **default**, so any member that states a value keeps it.

### Move 4 — the interactive paths get a reasoning floor

`wizard.py:222` drops `max_output_tokens` entirely (inherit the resolved default) and
its `timeout_seconds` becomes `INTERACTIVE_REASONING_TIMEOUT_SECONDS = 240` when
`is_reasoning_model` matches the wizard's route, 120 otherwise. `routes/coding.py:1879`
and `:2170` change their `min(configured, 120)` clamp to `min(configured, 240)` on a
reasoning route, unchanged otherwise.

240 s is ~2.4× the ~100 s measured thinking turn at an 8192 budget — headroom on a
contended GPU while still bounding a human's wait. It is not the 300 s loop floor: an
interactive chat that hangs for five minutes is a worse product than one that reports a
timeout. If SPEC-41 Move 1 lands, `think:false` collapses these turns to ~33 tokens and
the clamp stops mattering; this spec must not depend on that.

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
may now generate to 8192 — on this box roughly 5 minutes, which is inside the 600 s loop
timeout but outside the 240 s interactive clamp. That is a real regression *for that
turn*, and the correct one: a timeout is a legible failure, a truncated trace is not.

**`is_reasoning_model` is a substring heuristic.** It matches `"thinking"`, `"o1"`,
`"reasoning"` anywhere in a model id (scheduler.py:143-146). Any route named for a
"thinking" preset gets 8192 whether or not it reasons. Moving the function does not
improve it, and this spec does not touch the marker list — but the blast radius grows,
because a second dispatch path now consults it.

**Non-reasoning models are untouched, on purpose.** Item raised in review: raising
everything wastes latency and VRAM. The gate is the same marker test the scheduler
uses, so a `qwen2.5-coder:7b` team keeps 2048 exactly.

**A related, larger defect is deliberately left alone.** `max_output_tokens` is a hard
cap for hosted API providers too — `async_anthropic.py:104`, `async_openai.py:63-69`,
`async_google.py:93-94`. Today's 2048 default therefore also clips a long DEV diff on
an `anthropic.*` or `openai.*` coding route. Same defect class, but there is no
measurement behind a new number and the cost profile is different (paid tokens). Named
in Out of scope.

## Escape hatches

One knob on `CodingAutonomyPolicy`:

| Knob | Default | `False` restores |
|---|---|---|
| `reasoning_output_budget` | `True` | `max_output_tokens=2048`, `timeout_seconds=600` on every loop turn — today's trace exactly |

**How it reaches the turn, given the seam takes no policy.** Exactly the `dev_repo_read`
route (runner.py:5305 ← runner.py:7825): `CodingRunner.run` passes
`reasoning_output_budget=policy.reasoning_output_budget` into `build_run_turn`, which
applies it inside the shadowing `caller` at runner.py:5350 — the wrapper that already
sees every model exchange — by stamping the resolved `turn_limits` onto the per-turn
member copy before delegating to `_raw_caller`. When the knob is `False` it stamps the
legacy `{"max_output_tokens": 2048, "timeout_seconds": 600}` explicitly, and Move 3's
"explicit always wins" rule reproduces today's request byte-for-byte.

**Where the knob does not reach, stated plainly.** The Wizard, pm-ask, the directive
interpreter, the delivery-review route and the three `scripts/validate_*.py` entry
points call `gateway_member_caller` with no policy in scope. They get Moves 3-4
unconditionally. This is acceptable because what they receive is a *default*: an
operator who wants the old numbers on those paths sets `turn_limits` explicitly on the
member (already supported, already surfaced in the editor). Inventing a second,
policy-free global switch to cover them would be a config surface with no consumer.

## Definition of done

- `_is_reasoning_model` and the three constants live in one leaf module; `scheduler.py`
  imports them and its behaviour is unchanged (existing scheduler budget tests pass
  untouched).
- A PM/REVIEWER turn on a `local.qwen3.5:9b` team — the *unbound* member shape, no
  `model` key — resolves `num_predict = 8192`. This is the test that would have caught
  the bug: asserting it via a bound DEV member only would pass even with Move 2 missing.
- A member with an explicit `turn_limits.max_output_tokens` gets that value, reasoning
  model or not.
- A non-reasoning route (`local.qwen2.5-coder:7b`, `claude_cli.opus`) still gets 2048 /
  600 s.
- `reasoning_output_budget=False` produces a `LocalCouncilModelRequest` field-identical
  to today's for every loop turn.
- The Wizard's PM turn no longer sends `max_output_tokens: 2048`, and its timeout is
  240 s on a reasoning route.
- Recorded in the spec's landing note: a live run on the reference box with an all-local
  reasoning team reaches a REVIEWER verdict with `done_reason == "stop"`, i.e. the
  §4.1a arm that currently fails is green through the real coding path — not through
  `model-eval/` scripts.

## Out of scope

- **SPEC-41's four moves.** `think:false`, `format: "json"`, the loud-truncation guard,
  and local size tiers all sit on top of this. Move 2 there specifically must not land
  before this ships (see "Why this must land first").
- **Raising the default for hosted API providers.** Real, same defect class, no
  measurement, paid tokens — its own change.
- **Per-role or per-task-type budgets.** A DEV diff turn and a REVIEWER verdict plausibly
  want different budgets; nothing measured says what they are.
- **Improving the marker heuristic** (a capability flag on the route/catalog instead of
  substring matching on the model id) — moved verbatim here, worth doing, not here.
- ~~**The empty-`model` observation.**~~ **FIXED SEPARATELY, 2026-08-06.** On the
  non-worker turn paths (`runner.py:5508`, `:5635`) the member carried no `model`, so
  `runner.py:7620` sent `model=""` to `/api/chat` for a local PM/review turn. Verified
  against the reference box: Ollama answers `HTTP 400 {"error":"model is required"}`,
  which `_ollama_dispatch` maps to `FatalError("gateway_4xx: 400")` (the message lacks
  "not found", so it is not even reported as `model_not_found`). Fixed by
  `runner._member_model_id`, which derives the id from the `gateway_route_id` suffix
  using `bind_member_route`'s own derivation; regression tests in
  `tests/coding/test_unbound_member_model_id.py`. This spec still reads the route id
  rather than `member["model"]` — correct independently, and it keeps the budget
  resolver working for a member that is unbound for any other reason.

## Validation sequencing — do not reorder

The local path has been evaluated through **two** plumbing defects, so any prior
"we ran it locally" evidence is uninterpretable. Both must land before the outstanding
model-quality questions can be answered:

| # | Defect | State |
|---|---|---|
| 1 | Unbound members send `model=""` → every PM-governance / governance-review turn of an all-local team hard-fails | **fixed** (`_member_model_id`) |
| 2 | Coding turns send `num_predict=2048` vs a measured 2197-token mean → every reasoning turn truncates | **this spec** |

**Only after both:**

* **Re-test F001's judge-schema failure.** F001 records `qwen3.5:9b` "emitting
  wrong-schema JSON" and proposes a 15 GB `mistral-small3.1` judge. That observation may
  be an artefact of defect 1, defect 2, or neither. Run a real coding turn on the
  reference box with `qwen3.5:9b` in a judge seat and score whether it persists. If it
  vanishes, the judge recommendation is unfounded on the coding path too; if it
  persists, it stands on its own merits and the truncation must stop being cited as the
  explanation.
* **The benchmark's own highest-value follow-up** — a real council run scored on
  merge-gate pass rate rather than isolated function correctness — can share that
  harness. It is also the only thing that can answer whether `think:false` and
  `format:"json"` cost verdict *usefulness* rather than merely fixing shape.

Running either earlier produces a number that cannot be attributed.
- **A VRAM-fit check** for the larger generation window — belongs with model
  availability, as SPEC-41 already notes.
