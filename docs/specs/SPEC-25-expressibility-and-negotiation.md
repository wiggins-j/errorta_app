# Spec 25 — Expressibility and negotiation

**Source:** `docs/specs/ROADMAP-autonomy.md` Phase 2 — gap **G1, "enforcement
without negotiation"**; the 2026-07-26 #2 gravity-golf run (`no_progress` with a
merged game loop and 2 PRs open); and the three point fixes shipped as Spec 21.
**Target version:** v0.1 (engine)
**Status:** proposed
**Owner:** wiggins-j

> Every layer of the Spec 12–21 batch **constrains** an agent. Not one of them
> gives an agent a way to say *"this constraint is wrong for my situation"* or
> *"I cannot express what I need to do."* Four unsatisfiable-constraint bugs have
> shipped and been fixed one at a time. This spec fixes the **class**.

---

## Problem

The batch added task lint ([Spec 15](SPEC-15-capability-aware-planning.md)), the
revise breaker ([Spec 16](SPEC-16-revise-chain-circuit-breaker.md)), lane
partitioning (GL02), a bounded context channel (Spec 20), and a schema
relaxation (Spec 21). Each is correct. Together they produce a system where an
agent's only legal move is compliance, and where a *state the schema cannot
express* is indistinguishable from *an agent doing nothing*.

**Four shipped instances, all fixed individually:**

1. **`done=false requires at least one task`** (`schemas.py`,
   `PMPlanIntent._done_rules`) made *"prune duplicates, add nothing, not done"*
   inexpressible. The PM diagnosed the duplicate correctly, tried four times to
   say so, was rejected each time, and each rejection incremented `pm_idle` until
   `pm_idle_limit` (default **2**, `autonomy.py:71`) stopped a run with two PRs
   open. Spec 21 relaxed the rule to accept a decision
   (`schemas.py:145-149`) — a **point** fix.
2. **`PMDecision.title` required** while models emit `{"decision": ...}` and
   `model_config = {"extra": "ignore"}` (`schemas.py:97`) silently dropped it →
   `missing decisions[0].title` on 3 of those 4 retries. Spec 21 added a synonym
   alias (`schemas.py:102-121`) — a **point** fix.
3. **The original gravity-golf wedge**: a reviewer demanding execution evidence
   *no role can produce* (`_ROLE_TOOLS`, `turn_controller.py:27-32`, grants the
   DEV exactly `code_write` and every other role nothing). Spec 12/15 fixed that
   instance by routing execution to the gate.
4. **The exhausted DEV.** The tool catalog tells a DEV without repo read to
   *"emit a context_request intent"* (`turn_controller.py:101-104`); once Spec
   20's budget is spent the prompt says *"do NOT ask again"*
   (`runner.py:1927-1931`).

**And the fifth is already in the tree.** Item 4's exhausted-DEV text ends:

> *"If you truly cannot proceed, say so in your summary rather than asking
> again."* — `runner.py:1930-1931`

`DeveloperToolPlanIntent` has no `summary` field and `extra="ignore"`
(`schemas.py:169`), so that sentence is discarded — and the corrective prompt
tells the same model *"Drop unmodeled fields such as summary"*
(`runner.py:1279`). The one instruction we give a dev that has run out of
channel points at a field we throw away. Nobody wrote that bug; it *assembled
itself* out of two individually-correct strings, which is precisely why point
fixes do not stop the class.

### The four structural facts behind all five

**(a) A worker has no legal "I am blocked" turn.** Trace a DEV that genuinely
cannot proceed:

| It emits | What happens |
|---|---|
| `tool_plan` / `implementation` with no calls | `_tools_required` rejects it (`schemas.py:184-188`) |
| `tool_plan` / `investigation` with no calls | passes schema → no writes → empty diff → `no_net_change`, `unproductive=True` (`runner.py:5296-5314`) |
| `context_request` over budget | `context_request_exhausted`, `unproductive=True` (`runner.py:5153-5183`) |
| anything unparseable | `dev_turn_rejected`, `unproductive=True` (`runner.py:5114-5127`) |

Every path is `unproductive=True`, which feeds `_handle_unproductive`
(`autonomy.py:1513`) — model escalation, member exclusion, PM assist, then a
blocking `worker_unproductive` Problem. **Honesty and failure are the same
signal.**

**(b) The landing pad for honesty exists and is unreachable.**
`TurnOutcome(kind="task_blocked")` is handled by `_apply_outcome`
(`autonomy.py:2393-2397`), resets `pm_idle` **and** `plan_streak`, counts as
progress in `_completion_streak_reset_by` (`autonomy.py:1740`), and calls
`rec.block_task` (`topology.py:636-643`), which records a `blocked` decision the
PM reads. Grep `runner.py`: **no turn shape ever produces it.** The reconciler
has had a first-class "this cannot proceed, PM please look" transition all along
and no agent can reach it.

**(c) A shape rejection is accounted as idleness.** A rejected PM turn returns
`TurnOutcome(kind="planned", made_progress=False)` (`runner.py:4786-4794`), and
`_apply_outcome` increments `pm_idle` on exactly that
(`autonomy.py:2326-2330`). *Trying to comply accelerates termination.* Spec 21
fixed the schema and left this untouched — and left a second instance behind:
`made_progress=len(created) > 0` (`runner.py:4833`) means the decision-only turn
Spec 21 just legalized **still** increments `pm_idle`. The PM can now say "prune
the duplicates" and still be killed for saying it, two turns later.

**(d) A rejection teaches the validator's internals, not the shape.**
`parse_coding_turn` returns `f"invalid {role} intent: {exc.errors()[:3]}"`
(`schemas.py:619-620`), and `_corrective_turn_prompt` (`runner.py:1265-1280`)
splices that verbatim into the retry prompt. What the gravity-golf PM was shown:

```
[{'type': 'value_error', 'loc': (), 'msg': 'Value error, done=false requires
at least one task', 'input': {...}, 'url': 'https://errors.pydantic.dev/...'}]
```

That names what is forbidden. It never shows what is accepted — with
`_INTENT_CORRECTIVE_RETRIES = 1` for the PM (`runner.py:1234`) there is exactly
one attempt to guess it.

## Goals

- **No reachable state leaves an agent with no legal turn.** Stated as an
  invariant, enforced by a test, for every role.
- A typed, bounded, **answerable** channel for *"I need capability X"* /
  *"I cannot express Y"* — modelled on the DEV's existing read-only precedent,
  `DeveloperContextRequestIntent` (`schemas.py:200-221`).
- A turn rejected for **shape** is not a turn that made no **progress**. The two
  are accounted separately, and each is separately bounded.
- A corrective prompt **teaches the accepted shape**.

## Non-goals

- **Not letting an agent disable a guard or grant itself a tool.** Enforcement
  stays exactly where it is: `allowed_tools_for_role` /
  `CodingTurnController.execute_dev_turn` (`turn_controller.py:35`, `:161-177`).
  A capability ask is a *request recorded for a human-or-PM decision*; nothing in
  this spec mutates `_ROLE_TOOLS` or a policy flag at runtime.
- **Not free-form negotiation dialogue.** One typed intent with a closed reason
  enum and a bounded budget. No multi-turn argument, no rebuttal channel.
- **Not a new role.** No arbiter, no ombudsman. Worker asks are answered by the
  PM (which already re-plans and re-scopes); PM asks are answered by the run
  governor via the existing `attention` alarm surface.
- **Not retuning `pm_idle_limit` or any other threshold.** Item 3 changes *what
  counts*, not what it equals — a threshold retune is what produced this state.
- **Not the visibility half of G1's sibling gap.** Telling the PM that a detector
  is 6-of-8 into a countdown is [SPEC-24](ROADMAP-autonomy.md); this spec assumes
  nothing about it and composes with it if it lands first.

---

## Item 1 — `blocked`: an always-legal turn for every role

**Design.** One new intent in `schemas.py`, accepted for **every** role:

```json
{"schema_version": "coding_turn.v1", "role": "<any>", "task_id": "<assigned>",
 "intent": {"kind": "blocked",
            "reason": "missing_capability | missing_context | contradictory_instruction
                       | waiting_on_other_work | cannot_express_intent | other",
            "detail": "<what you cannot do and why, in one or two sentences>"}}
```

```python
class BlockedIntent(BaseModel):
    model_config = {"extra": "ignore"}
    kind: Literal["blocked"]
    reason: Literal[...] = "other"
    detail: str                       # the ONLY requirement
    needs: Optional[CapabilityAsk] = None   # Item 2
```

**The single validator is `detail` non-empty.** That is the whole point: a
`BlockedIntent` is constructible from *any* state a model can be in, with no
cross-field rule that another state could contradict. Compare every other intent
in the file — each carries a `model_validator(mode="after")` asserting a
*relationship* between fields (`schemas.py:132`, `:184`, `:217`, `:350`, `:377`),
and every relationship is a state some run will eventually land in.

**Dispatch.** `parse_coding_turn` already discriminates by `kind` for the DEV
(`schemas.py:601-615`); generalise that check to run for **all** roles *before*
the `_INTENT_BY_ROLE` lookup, so `kind == "blocked"` routes to `BlockedIntent`
whatever the seat. Same mechanism, one indent level out.

**Where it lands.**

- **Worker roles (dev / reviewer / tester)** → `TurnOutcome(kind="task_blocked",
  task=task, reason=<reason>:<detail>)`. This is the transition that already
  exists and is currently unreachable (`autonomy.py:2393`, `topology.py:636`):
  the task goes `blocked`, a `blocked` decision is recorded with the agent's own
  words, `pm_idle` and `plan_streak` reset, and the PM sees it in the backlog.
  **`unproductive` is NOT set** — that is the entire behavioural change. An
  honest block stops feeding the F127 ladder.
- **PM** → `TurnOutcome(kind="planned", made_progress=False)`, i.e. it **does**
  count toward `pm_idle`. A PM saying "nothing to add" *is* the idle state, and
  we are not making the run immortal — we are making the idle state
  **legible**. Where today `no_progress` fires on four rejected turns with no
  recorded reason, it now fires on two honest ones with the PM's reason on the
  ledger, which is exactly the input [SPEC-23](ROADMAP-autonomy.md)'s last-word
  turn consumes.

**Rendered into the prompts.** `_pm_prompt_segments`' `instructions` block
(`runner.py:2347-2375`) and the worker tool-catalog text
(`turn_controller.py:85-122`) gain one sentence naming the escape shape with a
literal example. The exhausted-DEV line (`runner.py:1930-1931`) stops pointing at
the discarded `summary` field and points here instead — fixing bug #5 by
construction rather than by noticing it.

**Δ note — why one shared intent rather than relaxing each role's validator.**
Relaxation was the Spec 21 move and it does not compose: every relaxation widens
the *normal* shape, so the next validator to fire is a different rule in a
different model, and the invariant ("some turn is always legal") is never stated
anywhere — it is an emergent property of five independent validators, which is
how we got here. One intent whose legality is unconditional makes the invariant a
single, testable object. It also gives the accounting layer a *typed* signal to
read (Item 3) instead of inferring intent from an absence.

**Acceptance.** For every role in `_INTENT_BY_ROLE`, a minimal `blocked` envelope
parses to a `ParsedTurn`. A blocked DEV turn marks the task `blocked` with the
dev's reason on the ledger and does **not** increment `unproductive_counts`. A
blocked PM turn increments `pm_idle` exactly once and records its reason. No
existing turn shape changes behaviour.

## Item 2 — `needs`: a typed, bounded, answerable capability ask

**Design.** The optional `needs` block on a `BlockedIntent`:

```python
class CapabilityAsk(BaseModel):
    model_config = {"extra": "ignore"}
    capability: Literal["execution", "repo_read", "context", "write_scope", "other"]
    what: str          # "a way to run pytest and see the output"
    why: str = ""      # bound to THIS task
```

Modelled directly on `DeveloperContextRequestIntent` (`schemas.py:200-221`), the
one channel in the system that already works: **typed** (a closed enum, not
prose), **bounded** (Spec 20's `_CONTEXT_REQUEST_LIMIT = 3`,
`runner.py:1837`), **answered** (`_answer_dev_context_request`,
`runner.py:1761`), and **recorded** (a `context_request` decision the team log
renders, `team_log.py:151-152`). Every one of those four properties carries over.

**Who answers.**

- **A worker's ask → the PM.** Recorded as a `capability_ask` decision, and
  surfaced in the next PM prompt by a `_capability_ask_note(store)` built as the
  exact sibling of `_capability_refusal_note` (`runner.py:2084-2111`) and
  `_duplicate_rejection_note` (`runner.py:2050-2081`) — read the decisions, keep
  only asks whose task is still open, render a bounded note. The PM's answer is
  an ordinary plan turn: re-scope the task, register a test command so a gate
  exists (`capability: "execution"`'s real answer under
  [Spec 15](SPEC-15-capability-aware-planning.md)), split the work, or drop it —
  each recorded as `capability_ask_answered` with the choice. The PM cannot
  grant a tool, and is told so in the same note.
- **The PM's own ask → the run governor.** `attention.raise_capability_gap_alert`
  (`attention.py:804`) already exists for GL03; a PM ask raises the same
  non-blocking Alert with `source="pm_ask"`. The run continues; a human sees it.

**Bound.** `_CAPABILITY_ASK_LIMIT = 2` per `(role, task_id)`, persisted on the
task exactly as Spec 20 persists `context_request_attempts`
(`_context_attempts_of`, `runner.py:1857-1872`), with the same defensive read —
`update_task(**patch)` routes unknown keys into `_extras` unvalidated, so a
non-numeric value must degrade to the default rather than raise inside prompt
composition. A **verbatim repeat** of an already-answered ask short-circuits to
exhausted, reusing `_context_question_key`'s hash-fold
(`runner.py:1842-1854`); the hash, not the text, is persisted, for the reasons
that comment already gives. Past the limit the *block* still stands (Item 1
guarantees the turn is legal) but the `needs` block is dropped and a
`capability_ask_exhausted` decision is recorded — the agent keeps its voice and
loses its megaphone.

**Composition with GL03.** `confabulation_from_failure`
(`capabilities.py:272-314`) infers a capability gap from a role *inventing* a
tool (`run_tests` on a gate-less role → `gap(execution)`), and
`GAP_ESCALATION_THRESHOLD = 2` (`capabilities.py:235`) gates the alarm. A typed
ask is the **honest form of the same signal**, so the two must not double-page
the PM: an ask and a confabulation for the same `(role, capability)` **dedupe**
onto the same alert, and a recorded `capability_ask` satisfies the threshold
immediately (asking once is already systematic — the guess-twice heuristic exists
only because a guess is ambiguous). This also creates the incentive we want:
asking is cheaper and louder than confabulating.

**Composition with Spec 15's manifest.** The ask's `capability` enum is the same
vocabulary `RoleCapability` carries (`capabilities.py:38-57`): `execution` maps to
`can_execute`/`gate_available`, `repo_read` to `repo_read`, `write_scope` to
`tools`. An ask whose capability the manifest says the role **already has** is
answered mechanically — no PM turn spent — with a decision pointing at the
granted surface. That is the Spec 17 corrective-hint idiom applied to asks.

**Acceptance.** A DEV blocked with `needs: {capability: "execution", ...}`
records one `capability_ask`, appears in the next PM prompt, and takes no
second turn to do so. A second identical ask on the same task is refused with
`capability_ask_exhausted` while the block itself still parses. An ask for a
capability the manifest already grants is answered without a PM turn. An ask and
a GL03 confabulation for the same `(role, capability)` raise one alert, not two.

## Item 3 — A shape rejection is not a progress failure

**Design.** Two changes, each small, each load-bearing.

**(a) Separate the counters.** Add `TurnOutcome.schema_rejected: bool = False`
and `LoopCounters.schema_rejects: int = 0`. The PM rejection path
(`runner.py:4786-4794`) sets `schema_rejected=True`; `_apply_outcome`
(`autonomy.py:2326-2330`) increments `schema_rejects` instead of `pm_idle` when
it is set. `plan_streak` is likewise untouched by a rejected turn — a turn that
never parsed is not a plan.

**The bound, so this cannot become an infinite retry:** a new
`schema_reject_limit: int = 3` on `CodingAutonomyPolicy` (beside
`pm_idle_limit`, `autonomy.py:71`, and mirrored in `policy_to_dict` /
`policy_from_dict`, `autonomy.py:257`, `:308`, per the F145 canary). Past it, the
run stops on a **new, honest** reason — `schema_unsatisfiable` — rather than
`no_progress`. That distinction is the entire diagnostic value: `no_progress`
sent three debugging sessions looking at the PM's *judgement*, when the defect
was in our *schema*. `schema_rejects` resets to 0 on any turn that parses, so a
transient malformed response costs nothing. The DEV/worker side needs no new
counter — `_handle_unproductive` (`autonomy.py:1513`) already bounds it, and
Item 1 removes the honest-block traffic that was inflating it.

**(b) A decision-only PM turn is progress.** `runner.py:4833` becomes
`made_progress = bool(created) or bool(new_decisions)`, where `new_decisions`
counts decisions whose title is not already recorded for this project — the
exact turn Spec 21 legalized (*"prune duplicates, add nothing, not done"*) and
then let `pm_idle` kill anyway. Novelty-gating keeps a PM from re-emitting one
decision forever; `plan_streak_limit` (**6**, `autonomy.py:134`) remains the
backstop for a PM that plans and re-plans while no worker ever runs, and it is
untouched.

**Acceptance.** Replay the gravity-golf sequence — four PM turns rejected for
shape, two PRs open — and the run does **not** stop; the counter that moves is
`schema_rejects`, and the recorded reason names the schema. Four *parsing*
turns that create nothing and decide nothing still stop the run at
`pm_idle_limit` (regression lock: the detector must still work). A PM turn
carrying only a novel decision resets `pm_idle`. A PM turn re-emitting a
decision it already recorded does not.

## Item 4 — Corrective prompts teach the accepted shape

**Design.** `schemas.py` gains one table and one accessor:

```python
def minimal_valid_example(role: str, kind: str | None = None) -> str
```

returning a **minimal valid** envelope for that `(role, kind)` — the smallest
JSON that passes `parse_coding_turn`. `_corrective_turn_prompt`
(`runner.py:1265-1280`) is restructured to show, in this order:

1. one plain-language line: *what was wrong* (`parsed.code.value` plus a
   humanised reason — "your plan turn had neither a task nor a decision", not
   `[{'type': 'value_error', 'loc': (), ...}]`);
2. **the minimal valid example** for the role it is being re-prompted as;
3. **the escape shape** from Item 1, verbatim, with the standing note that it is
   always accepted.

The raw Pydantic dump does **not** disappear — it moves to where it belongs, the
`{role} turn corrective retry` decision already recorded at
`runner.py:4301-4308`, where an operator debugging the run can read it and a
model cannot be confused by it. `_governance_corrective_prompt`
(`runner.py:1246-1262`) gets the same treatment; it already hand-rolls a partial
version of this (it restates the verdict schema inline), which is evidence the
idea is right and evidence that hand-rolling it per-callsite drifts.

**Δ note — one table, two consumers.** `minimal_valid_example` is the *same*
table Item 5's test enumerates. That is deliberate, and it is the repo's
established anti-drift idiom (Spec 19 Item 1's version mirrors; Spec 15 Item 1's
derived manifest; `test_f145_pm_reference`'s policy canary): a corrective prompt
that teaches a shape no longer accepted is worse than a raw dump, so the string
the model is taught and the string the test asserts must be one object.

**Acceptance.** A PM turn rejected for `done=false` with nothing in it is
re-prompted with a concrete valid plan envelope **and** the blocked shape, and no
Pydantic internals. The dump is still recoverable from `errorta decisions`. The
PM prompt golden (`python/tests/coding/test_prompt_segments_golden.py`) is
updated deliberately.

## Item 5 — The schema-satisfiability invariant test

**Design.** `python/tests/coding/test_spec25_expressibility.py`. This is the item
that prevents bug #6, and it is the reason the other four are worth building.

1. **The invariant, stated.** For every role in `_INTENT_BY_ROLE`
   (`schemas.py:389-394`), a *"nothing to do / blocked"* turn is constructible
   and `parse_coding_turn` accepts it. Table-driven over the role list itself, so
   **adding a role to `_INTENT_BY_ROLE` without an expressible blocked turn fails
   the build** — the mechanism that makes this a class fix rather than a fifth
   point fix.
2. **The example table is valid.** Every `minimal_valid_example(role, kind)`
   round-trips through `parse_coding_turn` successfully. A corrective prompt can
   never teach a rejected shape.
3. **The four historical instances, locked as regressions.**
   - *"not done, no tasks, one decision"* parses (Spec 21's fix, re-locked here
     because Item 3(b) now depends on it);
   - `{"decision": "...", "rationale": "..."}` parses as a `PMDecision`;
   - a DEV that cannot proceed has a turn that is neither rejected nor
     `unproductive` (Item 1);
   - an exhausted DEV is instructed toward a field that **exists** — a string
     assertion that the budget text (`runner.py:1927-1934`) names the blocked
     intent and does *not* say "summary", since `DeveloperToolPlanIntent` has no
     such field. This is bug #5, locked before it ever costs a run.
4. **The anti-drift lock.** Enumerate the `model_validator(mode="after")`
   validators across the intent models; for each intent kind, assert at least one
   instance in the example table satisfies every one of them. A new cross-field
   rule that makes some role's blocked/minimal turn unsatisfiable fails here,
   with a message naming the validator.

**Acceptance.** The suite passes on today's tree with Items 1–4 applied, and
fails if any of: a role is added without a blocked turn; a validator is added
that no example satisfies; the corrective table drifts from the schema; the
exhausted-DEV text regresses to naming an unmodeled field.

---

## Implementation notes

- **`python/errorta_council/coding/schemas.py`** — new `BlockedIntent` +
  `CapabilityAsk`; generalise the `kind` discrimination at `:601-615` to all
  roles; `minimal_valid_example` + its table; export both in `__all__`
  (`:625-631`).
- **`python/errorta_council/coding/runner.py`** — worker blocked branch →
  `task_blocked` (near the dev dispatch at `:5109-5192`, the reviewer at
  `:5345`, the tester path); PM blocked branch beside `:4786-4794`;
  `schema_rejected=True` on the PM rejection at `:4794`; `made_progress` at
  `:4833`; `_capability_ask_note` beside `_capability_refusal_note` (`:2084`) and
  wired into `_pm_prompt_segments` (`:2338-2396`); corrective prompts at
  `:1246-1280`; the exhausted-DEV text at `:1927-1934`; new
  `_CAPABILITY_ASK_LIMIT` beside `_CONTEXT_REQUEST_LIMIT` (`:1837`).
- **`python/errorta_council/coding/autonomy.py`** — `TurnOutcome.schema_rejected`
  (`:722-745`); `LoopCounters.schema_rejects` (`:748-827`);
  `schema_reject_limit` on the policy (`:71` neighbourhood) + `policy_to_dict` /
  `policy_from_dict` (`:257`, `:308`); the `_apply_outcome` branch (`:2326-2330`);
  the new `schema_unsatisfiable` stop reason checked beside the two `pm_idle`
  sites (`:1885`, `:2272` — the sequential and concurrent loops must agree, per
  the standing rule that a detector in only one path never fires where it is
  needed).
- **`python/errorta_council/coding/turn_controller.py`** — the escape shape in
  the tool catalog (`:85-122`). **No change to `_ROLE_TOOLS` (`:27-32`) or to
  `execute_dev_turn`'s allowlist (`:161-177`)** — the non-goal, in code.
- **`python/errorta_council/coding/capabilities.py`** — the ask/confabulation
  dedupe, reading `ConfabulationSignal` (`:238-249`) and
  `GAP_ESCALATION_THRESHOLD` (`:235`); no change to
  `confabulation_from_failure`'s classification.
- **`python/errorta_council/coding/attention.py`** — `raise_capability_gap_alert`
  (`:804`) gains the `pm_ask` source; no new alarm type.
- **`python/errorta_council/coding/team_log.py`** — render `blocked` /
  `capability_ask` decisions (`:151-152` is the existing `context_request` case).
- **New:** `python/tests/coding/test_spec25_expressibility.py`.
- **Import direction:** `capabilities` must not import `runner` (the `paths.py` /
  F159 discipline restated by Spec 15); the ask/confabulation dedupe therefore
  lives as a pure function in `capabilities` called from `runner`.

## Edge cases

- **An agent that spams the escape shape.** Bounded on both sides. A worker's
  block moves the task to `blocked`, so the *same* task cannot be blocked twice —
  it is no longer dispatchable until the PM acts. A PM's block counts toward
  `pm_idle` normally. The `needs` block has its own
  `_CAPABILITY_ASK_LIMIT = 2` with a verbatim-repeat short-circuit. Nothing here
  is unbounded, and nothing here creates a new termination path except the
  deliberate `schema_unsatisfiable` one.
- **An agent using "blocked" to dodge work.** The block is not free: it is
  recorded verbatim with the agent's reason (`topology.py:639-642`), the task
  state is visible to the PM and to `errorta status`, and a task blocked with a
  reason the PM judges spurious is re-opened by an ordinary plan turn. A team
  where *everything* is blocked is a backlog with no dispatchable head, which is
  Spec 10's `dispatch_wedged` detector (`_account_dispatch_wedge`) — already
  built, and now firing on an honest signal instead of a silent one. Genuinely
  empty PM turns still hit `pm_idle_limit`, unchanged.
- **[Spec 16](SPEC-16-revise-chain-circuit-breaker.md)'s breaker.** A blocked
  turn on a `revise:` task opens no PR, so it extends no revise lineage — the
  chain **ends** rather than deepening. `revise_chain_limit` (**3**,
  `autonomy.py:195`) and the `revise_chain_broken` accounting
  (`runner.py:971-994`, `revise_livelock_limit` **5**, `autonomy.py:199`) are
  untouched. Verify explicitly that a blocked revise task does not reset
  `last_broken_iter` (`autonomy.py:805-807`) in a way that postpones the livelock
  stop — a block is not a merge.
- **Spec 20's context budget.** `blocked` is the shape an exhausted DEV should
  reach for *instead of* a fourth `context_request`, which is the fix to bug #5.
  The budget itself is unchanged; `_CONTEXT_BUDGET_REARM` (`autonomy.py:1507`)
  keeps its current semantics. Note the ordering hazard: a dev that blocks with
  `needs: {capability: "context"}` while it still has budget left should be told
  so mechanically (Item 2's manifest check) rather than paging the PM.
- **A block that arrives with the task already merged/superseded** (concurrent
  loop): `block_task` on a terminal task must be a no-op with a recorded
  decision, not a state regression — the same discipline as
  `_requeue_stranded` (`autonomy.py:2430`).
- **[SPEC-23](ROADMAP-autonomy.md) lands first.** Then the PM's blocked turn is
  precisely the input its last-word turn wants, and `schema_unsatisfiable` should
  route through the same last-word path. Neither spec blocks the other; this one
  makes the last-word turn's *answer* expressible.
- **An operator reading `errorta status`.** `blocked` tasks with agent-authored
  reasons are new output. They are a feature (the run is telling you what it
  cannot do), but they must not read as errors — a blocked task is a question,
  not a failure.

## Testing

- **Item 1**: the per-role blocked-turn table (also Item 5 #1); a blocked DEV
  turn produces `task_blocked`, records the reason, and leaves
  `unproductive_counts` unchanged; a blocked PM turn increments `pm_idle` exactly
  once; every existing intent still parses unchanged (regression lock on
  `test_coding_schemas.py`).
- **Item 2**: one ask → one `capability_ask` decision → the note appears in the
  next PM prompt; a verbatim repeat → `capability_ask_exhausted` with the block
  still legal; an ask for an already-granted capability answers mechanically; an
  ask plus a GL03 confabulation for the same `(role, capability)` raises one
  alert (extends `test_gl03_capability_alarm.py`).
- **Item 3**: the gravity-golf replay (four shape-rejected PM turns, two PRs
  open) does not stop; `schema_rejects` moves and `pm_idle` does not; four
  parsing-but-empty turns still stop at `pm_idle_limit`; a novel-decision-only
  turn resets `pm_idle` and a repeated one does not; `schema_reject_limit`
  survives `policy_to_dict`/`policy_from_dict` round-trip (the F145 canary) and
  `0` disables the detector, matching every sibling limit.
- **Item 4**: a rejected PM turn's re-prompt contains a valid example and the
  blocked shape and **no** `pydantic.dev` / `'loc':` substring; the raw dump is
  present on the corrective-retry decision; the prompt golden is updated.
- **Item 5**: as specified above — the four regression locks and the
  validator-enumeration anti-drift check.
- Full coding suite + `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: the blocked intent and the capability ask, the
  new decision choices (`blocked`, `capability_ask`, `capability_ask_answered`,
  `capability_ask_exhausted`), the `schema_reject_limit` knob, and the rule that
  the PM answers capability asks by **re-planning or registering a gate** — never
  by granting a tool.
- `docs/CLI.md`: blocked tasks and capability asks appear in `errorta decisions`
  and `errorta status`; a `schema_unsatisfiable` stop is a **bug in the schema**,
  and the decision record carries the validator dump to file it with.

## Out of scope / follow-ups

- **Backfilling Spec 20/21 docs.** Both shipped code-only; this spec cites their
  code directly because there is nothing else to cite. Worth doing; not on the
  critical path (the roadmap says the same).
- **Letting the PM actually grant a capability** — registering a test command
  from a plan turn, so `capability: "execution"` has a mechanical answer instead
  of a re-scope. That is real, and it is
  [SPEC-26](ROADMAP-autonomy.md)'s grant-or-delete territory, not this spec's.
- **A blocked-turn quality signal** (was the block justified?). Requires
  judging content, which is a different kind of machinery; the PM's re-plan is
  the v1 arbiter.
- **Generalising `schema_unsatisfiable` into the stop-reason rework.** Spec 27
  converts stop reasons into control actions; `schema_unsatisfiable` should
  eventually narrow-then-continue rather than terminate.
