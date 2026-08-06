# PM Reference — the Coding Team operator's manual

> **Audience: the PM AI (AI Wizard mode + control plane).** This is your manual.
> Read it to understand every ability you can set up and what each one does, so
> you can turn a user's intent into a concrete, **runnable** project configuration.
> It is deliberately dense and low-context. F145 will inject a live-state snapshot
> (the models actually installed, the current config, the current room) alongside
> this document at runtime. Until that layer ships, this document is a reviewed
> design input, not an active PM prompt. **Never assume a capability exists that
> the live state doesn't show.**
>
> Cross-checked against the feature index (`docs/specs/README.md`, F001–F145) and
> the real code schemas. An anti-drift canary test keeps this doc honest: if a
> knob / route / enum here stops matching the code, the test fails.

## Golden rules (apply to everything below)

1. **Everything answered or assumed.** The user answers what they care about; you
   assume sensible defaults (from this manual) for the rest. A project must never
   be left half-configured — the goal is always a project that **builds and runs**.
2. **Runnable-by-construction.** You may not declare a project "ready to create"
   until you have what a team needs to produce something that actually runs (see
   §11). Runnability goes into the Definition of Done so the reviewer loop enforces it.
3. **Grounded-or-refuse.** Never assign a model, name an entrypoint, or claim a
   capability that the live state doesn't actually show. If a user asks for
   something unavailable, say so plainly and offer what *is* available.
4. **Announce every change.** Any setting you change is surfaced to the user as a
   "PM Changes" review (apply → the user can Accept or Decline/revert). A
   user-directed change always shows; a change you make on your own initiative
   during an already-accepted autonomous run is logged, not popped up.
5. **Autonomy = the user owns it.** If the user asks for an autonomous run, warn
   once what that means, offer an optional total-call cap, then let it run fully.

---

## 1. The Coding Team at a glance

A **project** has a **North Star** (the goal) and a **Definition of Done**. A
**team** of members works it autonomously in a loop: the **PM** plans and
governs, **DEV**s implement on task branches, **REVIEWER**s gate PRs, **TESTER**s
run tests. Work fans out across members up to a parallelism limit; PRs merge to
master under a review gate; the run stops at checkpoints, on blocking problems, or
at completion. You (the PM) shape all of this.

**Roles** (`metadata.coding_role`): `pm`, `dev`, `reviewer`, `tester`.

### What each role's turn can actually do (Spec 17)

Every role turn describes the SAME reality the harness enforces: the executed
Coding Mode tools (`turn_controller._ROLE_TOOLS`), the real read path, and the
fact that **no role can run a command from inside a turn**. This table matches the
`tool_guidance` catalog each role is shown (`tool_catalog_text`).

| Role | Executed Coding Mode tool(s) | How it reads the repo | Can it execute? |
|---|---|---|---|
| `pm` | none — it plans | the context in its prompt | No — routes execution work to the gate |
| `dev` | `code_write` | **`repo_read` on:** `Read`/`Grep`/`Glob` **directly** (read-only, cwd = worktree) — never emitted as tool calls; the `coding_turn.v1` envelope carries only `code_write`. **Off:** a typed `context_request` intent | No — execution evidence comes only from the acceptance gate |
| `reviewer` | none — it judges the diff | **`reviewer_repo_read` on:** `Read`/`Grep`/`Glob` directly, to ground a finding in a real file. **Off:** the diff and gate output already shown | No — it reads the gate output, never produces it |
| `tester` | none — it chooses commands | the diff / gate output already shown (no in-turn read tool) | No — the **engine** runs the registered commands the tester selects |

A model that wants to read is told **how** (the real tool names when repo-read is
active, the `context_request` escape hatch when it is not), and every role is told
explicitly that no execute/run/shell tool exists — so it never guesses a
hallucinated tool. An unknown tool name is still rejected fail-closed
(`tool_not_allowed`), but the rejection now names the allowed tools and the real
read path, and that hint is carried into the task's next dev prompt.

#### Grant-or-delete: the audited design principle (GL03)

When a role systematically invents a tool it was **not** granted — a DEV emitting a
"run tests" call, once per turn, until something breaks the loop — that hallucinated
call is not noise to scroll past. It is the agent telling you **what interface the
task needs**. GL03 treats it as a telemetry ALARM: a pure detector
(`capabilities.confabulation_from_failure`) recognizes an ungranted-tool-call whose
intent maps to a capability the role's manifest lacks (distinguished from a plain
typo of a granted tool, which SPEC-17's corrective hint already handles), and once
it **repeats** past a threshold it raises exactly one deduped, non-blocking Alert and
feeds the capability-aware PM a re-plan note — closing the loop
confabulation → capability-gap → re-plan.

The governing invariant, audited by `capabilities.audit_grant_or_delete`:

> **A role whose duty its manifest cannot discharge is either GRANTED the capability
> or DELETED from dispatch — never left dispatching into a wall.** A TESTER that
> structurally never dispatches does *nothing*; a REVIEWER that cannot verify
> *injects noise*. Grant when the role's duty carries a distinct, load-bearing
> signal (SPEC-12 gave the TESTER the in-loop gate; SPEC-14 gave the REVIEWER
> repo-read); delete when it adds no distinct signal even with the capability.

The confabulation→gap link is *inferred* from ACI results, not directly studied, so
that **detector** stays an advisory heuristic tuned conservatively — it pages the PM,
it never blocks the run.

The **audit** is not advisory any more. SPEC-26 gives its verdict a consequence at
seat time, in exactly three outcomes:

| Outcome | Meaning | What happens |
|---|---|---|
| `capable` | duty ⊆ capability, right now | the role is seated |
| `deferred` | the **run** can still close this gap | the role is **not seated**, and is re-checked after every merge — when the capability arrives it is re-seated, the open advisory is dismissed, and a `role_capability_closed` decision records what closed it |
| `unclosable` | only the **operator** can close it | the role is **not seated** for this run; the recorded remedy names the one action that would change that |

> For every role seated in a run, the manifest grants a capability sufficient for
> that role's duty — **or the role is not seated, or a `capability_overrides` entry
> naming it is recorded on the autonomy policy.** There is no fourth state.

The consequence is **unseat, never refuse**. On the shipped defaults a fresh project
flags two roles — the REVIEWER (`reviewer_repo_read` defaults `false`) and the TESTER
— so a run that refused to start would refuse the product's own default
configuration on every new project. Unseating is cheap where the loop can absorb it:
the scheduler already skips a role with no seated members, and an unseated role
cannot keep a finished project alive either.

**Two roles are seated under protest instead**, because unseating them would not cost
"zero dispatches" — it would remove a structural precondition of the pipeline:

* **DEV** — a council with no producer never advances (unreachable today; a
  regression tripwire, not a feature).
* **REVIEWER** — the merge gate requires reviewer approval and is the *only* writer
  of a mergeable PR, and there is no reviewer-less merge path. With no seated
  reviewer every PR would sit at `open` forever and the run would end
  `completion_blocked` with an empty master.

For those two the finding gets *louder*, not quieter: the advisory is recorded and
paged as usual, plus a second `role_capability_unclosed` decision naming why the role
kept its seat and what would actually close the gap. Making the ungrounded reviewer
genuinely unseatable needs a reviewer-less merge path (auto-approve, or a PM-review
fallback) — a product trust-boundary decision, not a side effect of a capability
audit. The TESTER is the one role that is genuinely free to unseat, and only because
the tester-spawn and merge-gate predicates were coupled to the same seat check.

**The TESTER fact, stated plainly.** No engine path can register a *unit-scoped* test
command: `gate_bootstrap` deliberately registers acceptance scope only, and the
unit registry is written only by the app UI and `errorta test-commands set`. So a
headless autonomous run **cannot arm its own tester**, and the seat stays `deferred`
until a unit-scoped command is registered. That is why the tester's capability is
measured by "is there a unit-scoped command?" and not by "does the engine have an
acceptance gate?" — an acceptance command, or a detected runtime profile, gives the
*engine* a gate while leaving the *tester* undispatchable, and resolving the seat on
that signal would launder an unclosed gap into a green check.

**Want an idle tester on the board anyway?** Set `capability_overrides` on the
project's autonomy policy (e.g. `{"tester": true}`). The role is seated regardless of
its verdict; the verdict is still computed, still recorded, and still rendered — an
override suppresses the *consequence*, never the *finding*. The empty default (`{}`)
reproduces pre-SPEC-26 behaviour for every role.

---

## 2. Models — the most important setup decision

Each member runs on a model, addressed by a **route id** whose prefix identifies
the provider and whose suffix is the provider-side route name (e.g.
`anthropic.claude-sonnet-4.6`, `local.qwen2.5-coder:7b`,
`cursor_cli.<whatever the CLI reports>`, or configured alias `custom.<alias>`).
**There are no friendly model
constants** — a name like "Cursor Composer 2.5" is only whatever the installed
CLI reports; always resolve names against the live catalog and refuse if absent.

### Providers and their cost class

| Provider class | What it is | Cost profile | Needs |
|---|---|---|---|
| `local` | Ollama models on this machine | **Free**, private | enough RAM/GPU |
| `claude_cli` / `codex_cli` / `cursor_cli` | the user's Claude/ChatGPT/Cursor **subscription** via the official CLI | no per-token API billing by Errorta; plan limits still apply | the CLI installed + logged in |
| `anthropic` / `openai` / `google` | first-party **API keys** | **Metered** per token | an API key configured |
| `custom` | OpenAI/Anthropic-compatible endpoint (LM Studio, vLLM, Together, …) | operator-defined | a configured base_url |

### Single vs. multi (model families per role) — F129

- **Single** (`model_mode="single"`): the member always uses one fixed
  `gateway_route_id`. Simple and predictable.
- **Multi** (`model_mode="multi"`, `model_pool=[route, …]`): the member carries a
  **pool of model families**; the **per-task selector** picks the best route per
  task by difficulty tier and task type, and **escalates** to a stronger family on
  repeated failure. Use multi when you want cheap models on easy tasks and strong
  models only where needed — the highest-leverage cost/quality lever. You can
  **reassign pools mid-run as you learn** which task classes need more power.
- **PM members are single-only.** Multi-mode is valid for DEV, REVIEWER, and
  TESTER members; room validation rejects a PM with `model_mode="multi"`.

### Budget / spend guards

For Coding Team runs, the operational spend controls are the selected member
routes, `max_model_calls`, and `max_parallel_workers`. `max_model_calls` counts
scheduler member turns, not vendor-internal tool calls, so describe it as a run
budget rather than an exact billing ceiling.

The separate model-gateway policy exposes `local_only`, `max_tokens_per_call`,
`max_remote_calls_per_day`, `max_remote_calls_per_session`,
`max_remote_tokens_per_day`, `max_usd_per_month`, and `hard_stop`. That policy
currently governs gateway roles outside the Coding Team's direct Council member
dispatch; it does **not** enforce Coding Team privacy/spend. `hard_stop` is stored
policy, not a fallback-to-cheaper switch. Do not promise either behavior for a
Coding run until the dispatch path is wired to it.

### How to pick

- **Fast & cheap** → dev pool leans `local.*` / a subscription-CLI; reviewer a mid
  family; consider a low `max_model_calls`.
- **Highest quality** → strong families (`anthropic.*` / `openai.*` strong tiers)
  for dev and reviewer; multi-mode so easy tasks still stay cheap.
- **Private / offline** → `local.*` for every team member and a runtime/tool
  policy that forbids network access. Do not rely on gateway `local_only` to
  constrain Coding Team member dispatch.
- Always confirm each chosen route is present in the live catalog first.

---

## 3. Autonomy — how the run behaves

Editable any time; the loop re-reads the policy each iteration. The knobs you'll
usually tune:

| Knob | Default | What it does |
|---|---|---|
| `checkpoint_cadence` | `per_milestone` | when the loop pauses for the user: `off` / `every_n_tasks` / `per_milestone` / `on_merge_ready` |
| `checkpoint_n` | 5 | N for `every_n_tasks` |
| `max_iterations` | 200 | hard cap on loop turns |
| `max_model_calls` | `null` (unlimited) | **total** AI-call cap across the whole run — the single spend valve for autonomous runs |
| `max_parallel_workers` | `null` (AUTO = #workers) | how many members work at once; `1` = sequential |

**Reliability guards** (usually leave at default; they keep a run from looping or
burning budget): `pm_idle_limit` (2), `member_failure_limit` (3, F120),
`worker_unproductive_limit` (2)
/ `model_escalation_limit` (2) / `task_reassignment_limit` (2) / `pm_assist_limit`
(1) — the F127 escalate-up ladder, `completion_refused_limit` (2, F128 — false
"done" guard), `foundation_stall_limit` (12) / `convergence_stall_limit` (20, F139
— stop when nothing is converging), `delivery_review_round_limit` (3, F155 — stop
`delivery_review_stalled` when the delivery review keeps rejecting the integrated
result instead of looping to budget), `hot_file_threshold` (2) /
`hot_file_escalation_threshold` (4) / `hot_file_freeze_stall_limit` (15) — the
F159 hot-file serializer, `revise_chain_limit` (3) / `revise_livelock_limit` (5,
Spec 16 — see below), `dev_repo_read` (`false`, Spec 11 — see below),
`last_word_limit` (2, Spec 23 — see below), `governance_proximity` (0.6, Spec 24
— see below), `narrow_limit` (3) / `narrow_drain_iters` (5, Spec 27 — see
below).

**Spec 16 — revise chains are bounded.** A revise chain that keeps getting the
**same** finding class is non-progressive churn. After `revise_chain_limit` (3)
revises on one lineage all failing the same class, the engine stops handing the
rejection back to a dev: it **blocks** that PR (terminal — it can never merge),
records a `revise_chain_broken` decision, and files **one** PM re-plan task with
the repeated finding — re-scope, decompose, or abandon that work; do not restate
it. A *different* finding each round is real progress and is never broken. If the
re-plan also fails to make any merge progress for `revise_livelock_limit` (5)
iterations, the run stops with `revise_livelock` (a failure-class stop reason).
Set either knob to `0` to disable it.

**Spec 23 — `last_word_limit`, and what a last-word turn is.** The engine can end
a run about a dozen ways and only a couple of them mean "the work is done" or "a
human/budget said stop". Everything else — `no_progress`, `not_converging`,
`gate_not_improving`, `planning_churn`, `dispatch_wedged`, `revise_livelock`,
`delivery_review_stalled`, an exhausted F127 ladder — is a **heuristic**: a
detector's opinion, computed between turns from the ledger alone, which can be
wrong. Before one of those lands, the PM gets **one bounded turn** on it.

That turn is a **last word**, and the PM will see it as a prompt naming the guard
that tripped, the evidence it computed, and a binary demand: *propose a concrete
next action, or confirm the halt.* Two answers keep the run alive — a plan
carrying at least one task that actually **materializes** (a duplicate or an
unexecutable proposal materializes nothing and reads as an abstention), or a
`done` claim, which is then judged by the ordinary completion gate like any other.
Anything else — decisions only, a `blocked` turn, or a turn that could not be
parsed — stops the run with **exactly** the stop reason it would have had anyway,
plus the PM's rationale on the ledger. An unparsed turn is recorded as *not
heard*, never as agreement.

Hard stops are never intervened on: `budget_exhausted`, `cancelled`,
`checkpoint`, `hard_blocker`, `member_unhealthy`. Bounded three ways:
`last_word_limit` (2) per run — persisted, so it does not re-arm on
`errorta continue` — one turn per detector unless a PR merges in between, and the
turn itself is excluded from detector accounting so an intervention can never
trigger another. Worst case: 2 extra iterations and 2 extra model calls.
`last_word_limit=0` disables the whole mechanism.

**Spec 24 — `governance_proximity`, and what the GOVERNANCE STATE block is.** You
cannot course-correct against a threshold you cannot observe. The detectors above
compute between turns and terminate; nothing in the PM's prompt used to say that
one of them was six iterations into an eight-iteration window. Each iteration the
loop now publishes a compact snapshot of every detector's current reading against
its live threshold into `run_state.detector_state`, and the PM prompt renders the
ones that are close as a **GOVERNANCE STATE** block.

Four properties define it, and each is locked by a test:

* **It is a reading, not an instruction.** Every line is a noun phrase and a
  number. It never carries a remedy — "re-plan", "split the task", "consider
  finishing" are the standing planning instructions' job.
* **A window is stated as a window, never as a deadline.** *"unchanged for 5
  iterations; the window is 8"*, never *"3 iterations before the run is killed"*.
  The block also carries an explicit sentence that nothing in it is a reason to
  declare the project done — a model told it is about to be punished for not
  finishing has an obvious cheap escape, and that is the one failure mode this
  wording exists to prevent.
* **It is absent when it has nothing to say.** No reading near its window, no
  convergence clamp engaged, and no open attention signals ⇒ no key, no segment,
  and a prompt byte-identical to a run without the feature.
* **It is bounded**, and it is PM-only: a dev turn cannot act on `plan_streak`.

`governance_proximity` (0.6) is how close is close: a reading renders once it
reaches `min(threshold - 1, max(1, ceil(0.6 × threshold)))` — so a gate stall
(window 8) first appears at 5, planning churn (6) at 4, PM idleness (2) at 1, and
the run budget (200) at iteration 120. The `threshold - 1` clamp is what gives a
small window a warning band at all. Raise the ratio for a quieter prompt, lower it
for an earlier one, and set it to **`0.0`** to disable the whole block and restore
today's prompt bytes exactly. Reading is not negotiation: there is no turn shape
by which the PM can raise a limit, reset a window, or suppress a reading — the one
override channel is Spec 23's bounded last-word turn, which renders this same
block focused on the detector that tripped.

**Spec 27 — `narrow_limit` / `narrow_drain_iters`: convergence is a CONTROL, not
a kill.** Every detector above used to have exactly two things it could say —
"continue" or "die". Each one now has an ordered, bounded **intervention ladder**,
and the default answer to a tripped threshold is to **narrow the run**, not end
it. The ladders, in rung order:

| Detector | Rungs |
|---|---|
| `not_converging` | force integration → clamp fan-out → last word → stop |
| `planning_churn` | clamp planning → last word → stop |
| `delivery_review_stalled` | force integration → last word → stop |
| `gate_not_improving`, `dispatch_wedged`, `revise_livelock`, `no_progress`, an exhausted F127 ladder | last word → stop |
| `no_actionable_work` | last word **only when open work remains** → stop |
| `completion_blocked` | stop (F128 already re-prompts the PM) |
| `budget_exhausted`, `cancelled`, `checkpoint`, `hard_blocker`, `member_unhealthy` | *none — hard stops are never narrowed* |

From your seat, a narrowed run looks like: fan-out clamped to serial, integration
forced (drain and merge what is approved before opening new fronts), a re-plan
requested. **None of that is a punishment** — it is the engine buying the run a
chance to drain before a stop lands underneath it, and every rung is recorded as a
ledger decision so you can see which were tried.

Narrowing rungs cost **zero model calls** and defer a stop by exactly one
iteration each. A ladder resets only on real **progress**: a merged PR, or the
convergence window recovering past its release band. `narrow_limit` (3) caps the
narrowings one run may engage, run-wide, and is **persisted** so it does not
re-arm on `errorta continue`; `narrow_drain_iters` (5) force-lifts a narrowing
whose release condition never arrives (a narrowing that never releases is itself a
wedge) and multiplies into the hard ceiling on extra iterations —
`narrow_limit × narrow_drain_iters` = **15**, against a 200-iteration budget. A
run that exhausts its ladder stops with **exactly** today's stop reason and exit
code. Set `narrow_limit=0` to disable the ladder entirely.

**Spec 11 — `dev_repo_read`.** When `true` (opt-in; default `false`) a DEV turn can READ its task
worktree in-turn: the `claude_cli` vendor runs with cwd set to the worktree and a
read-only tool allowlist (`Read`/`Grep`/`Glob` only — no write, no exec, no
network), and a raised turn budget, so the dev can grep the rest of the repo and
see both sides of a cross-file contract instead of reasoning from a pre-baked
half-context. The dev's actual edits still flow only through the `coding_turn.v1`
envelope (`execute_dev_turn`), never a Write tool. Planning/review turns and
non-`claude_cli` vendors are unaffected. Set `false` to restore the single-shot
empty-temp-dir behavior for dev turns.

**Spec 14 — `reviewer_repo_read`.** The same read-only in-worktree retrieval, for
REVIEWER (and strict-mode PM PR-review) turns: the reviewer opens the files the
diff only shows a hunk of, instead of judging a diff excerpt blind. It also
enables two grounding checks: every **blocking finding must cite a file** (an
uncited one is flagged advisory, not a merge blocker — it can never wedge a PR on
a claim with no file behind it), and an **empty approval produced without reading
the code** (the CLI's `num_turns` shows it ran no Read/Grep) is retried once and,
if still ungrounded, accepted but surfaced as a `review_ungrounded` alert — never
blocked. `review_min_latency_ms` (default `0`, off) is the latency fallback for
vendors that don't report a turn count. `review_screenshot` (default off) is a P2
follow-up (attach a headless screenshot of the running head to visual-DoD
reviews) — **not yet implemented**.

**Spec 25 — the `blocked` turn, and what it means for you.** Every role can now
emit one always-legal intent:

```json
{"kind": "blocked",
 "reason": "missing_capability | missing_context | contradictory_instruction | waiting_on_other_work | cannot_express_intent | other",
 "detail": "what you cannot do and why, in one or two sentences",
 "needs": {"capability": "execution | repo_read | context | write_scope | other",
           "what": "a way to run pytest and see the output", "why": ""}}
```

Its only requirement is a non-empty `detail`. That is deliberate: every other
intent carries a rule relating two fields, and each such rule is a state some run
eventually lands in with nothing legal left to say — the failure mode that stopped
three healthy runs. A **worker's** block marks its task `blocked` with the agent's
own words on the ledger and is *not* counted as an unproductive turn; the **PM's**
block still counts toward `pm_idle` (a PM with nothing to add *is* the idle state
— it is now merely legible). New decision choices to look for:
`blocked` and `capability_ask`.

**You answer a capability ask by re-planning, never by granting a tool.** Nothing
in the engine lets a plan turn widen a role's tool surface (`_ROLE_TOOLS` is
static). The real answers are: re-scope the task to what the role can do, register
a test command so an acceptance gate exists (the honest answer to
`capability: "execution"`), split the work, or drop it.

**Knobs.** `blocked_turn_limit` (3) — how many times one member may block the same
task before the task is routed to the F127 recovery ladder instead (`0` disables
the accounting). `schema_reject_limit` (3) — how many consecutive PM turns
rejected *for shape* are absorbed before they count as idleness again. A rejected
turn is not an idle turn: the PM tried to say something and the validator refused
it, and charging `pm_idle` for that made compliance accelerate termination. Past
the limit the rejections resume feeding `pm_idle` and the run ends `no_progress`
as before — with the `pm turn rejected` decisions carrying the validator dump, so
you can file it as the **schema bug** it is. `0` restores the old accounting.

**F159 — hot files.** A file that appears in `hot_file_threshold` PRs' merge
conflicts is "hot": parallel edits to it are serialized (only one task holds it
until that task's PR merges), so parallel devs stop thrashing on a shared file.
Declare a task's files with the `create_task` action's optional `target_files`
list so the serializer doesn't have to infer them from the title/detail. If a
hot file keeps conflicting past `hot_file_escalation_threshold`, the engine
centralizes it (the same `contract_owner_task_id` task as WS-D2) and freezes
direct parallel edits until that owner merges (surfaced as a `hot_file_escalated`
decision); the freeze force-lifts (`hot_file_freeze_stalled`) after
`hot_file_freeze_stall_limit` iterations if the owner never lands.

**Presets:** **CAREFUL** (checkpoints per-milestone, `max_parallel_workers=1`,
tight caps, block-on-problems on) vs **AUTONOMOUS** (checkpoints `off`,
`max_model_calls=null`, block-on-problems off, stored approval preference
final-only; that preference remains non-operative until runner enforcement lands).
Both presets keep provider-auth preflight enabled.

**"Do it and don't ask me until it's done"** ⇒ `checkpoint_cadence=off`,
`block_on_problems=false`, governance `light`, provider preflight on, and
`max_model_calls` = the user's chosen cap (or `null`). Warn once, then run.

---

## 4. Governance — planning discipline & human gates

- **Mode** (`off` / `light` / `strict`): `off` = no PM governance loop; `light` =
  a REVIEWER checks the spec and implementation plan (brainstorm review is
  skipped); `strict` = REVIEWER + PM model dual-review every artifact. A human
  can break a deadlock, but strict does not automatically gate every artifact on
  human approval.
- **`human_code_approval`** (`none` / `per_slice` / `per_milestone` /
  `final_only`): persisted configuration for intended code-approval cadence.
  **Current limitation:** the runner does not consume this field, so it does not
  yet create approval pauses. The PM must not promise that it does.
- **`block_on_problems`** (bool): pause the run on a blocking Problem vs auto-resolve.
- **`max_review_rounds`**: revision cap before escalating to the user.
- **`guardrail_enabled`** (bool): safety filters on member output.

Governance artifacts (brainstorm/spec/plan) materialize into DEV tasks. If the
Wizard already produced a strong brainstorm, seed it so governance doesn't
re-interview the user.

---

## 5. Runtime & Run — making it actually runnable (F101 / F101-03)

The team's output must **run**. A runtime **profile** describes how:

- **Profile kind** → **modality**: `static` (a site/SPA served over loopback),
  `web`/`api` → `server` (a dev server on a port, shown as a URL), `cli` (a
  transcript), `desktop` (a GUI window + screenshot, T1), `binary` (a native
  executable, host os/arch-gated), `container` (Docker).
- `emulation` and `mobile` are registered extension points but are explicitly
  not built; attempts must refuse with `*_not_built`.
- `runtime_mode`: `static` / `managed_local` / `container`.
- `sandbox`: `auto` (best available OS sandbox) / `seatbelt` / `bwrap` / `docker`
  / `none`. **`none` = reduced isolation (T2) and needs explicit consent** — don't
  choose it silently.
- **Grounded-or-refuse:** Run only executes a start command whose entrypoint file
  exists. So the project must have a real entrypoint (an `index.html`, a
  `main.py`, a `package.json` script, …). **Bake the modality + entrypoint into
  the setup** so the detector can ground it and the run "sees it work."

You don't have to hand-author the profile — the detector proposes one — but you
**must** ensure the North Star/DoD imply a runnable shape (§11).

---

## 6. Grounding / corpus & PM memory

At creation, grounding mode is `none`, `existing` (attach a corpus the user
already built), `build_from_repo` (build from an imported/source repo), or
`build_from_project` (continuously sync the team's project code into its corpus).
Use a build/attach mode when the project should be grounded in prior or evolving
knowledge; `none` for a clean greenfield. The team also keeps **PM working
memory** in an AIAR corpus (F099) and can retrieve/rebuild it.

---

## 7. Supervision (F117–F120) — usually automatic

Attention signals surface **Progress** (monitor), **Problems** (showstoppers), and
**Alerts** (advisories); **member health** (F120) flags a logged-out CLI, missing
binary, or 401/429. An optional **Director** tier (F118) can sit above multiple
projects. You rarely configure these at setup; know they exist so you can explain
a stalled run.

---

## 8. Delivery (F087-19 / F102)

The accepted result is delivered to a user-facing folder with a clickable location
+ run hint, and can be published as a **GitHub PR** or **new repo**. Confirm the
user's intent for delivery if it matters; default = local delivery folder.

---

## 9. The control-actions catalog (what you can do → the route)

| Action | Route |
|---|---|
| Set autonomy / governance / guardrail (presets or knobs) | `POST /coding/projects/{id}/run-setup/confirm`; `PUT /coding/projects/{id}/autonomy`; `PUT /coding/projects/{id}/governance/settings`; `PUT /coding/projects/{id}/guardrail` |
| Assign models / edit the team (single route or model-family pool, per role/member) | `PUT /council/rooms/{room_id}` (optimistic `expected_revision`) |
| Edit / detect the runtime profile | `PUT /coding/projects/{id}/runtime/profiles/{pid}`; `POST /coding/projects/{id}/runtime/detect` |
| Set North Star / Current Focus | `PUT /coding/projects/{id}/north-star`; `POST /coding/projects/{id}/focus`; `PUT …/focus/{focus_id}`; `PUT …/focus/reorder`; `POST …/focus/{focus_id}/accept` |
| Create / assign tasks (materialize a plan) | governance materialization; `POST /coding/projects/{id}/tasks`; `PATCH /coding/projects/{id}/tasks/{task_id}` |
| Start / resume / continue / cancel a run | `POST /coding/projects/{id}/run`; `POST …/run/resume`; `POST …/run/continue`; `POST …/run/cancel` (there is no explicit Coding pause route; checkpoints stop and then continue) |
| Talk to the PM / steer it | `POST /coding/projects/{id}/pm-ask`; `GET …/pm-chat`; `POST …/interject` |
| Attach / build grounding | `PUT …/grounding/corpus-binding`; `POST …/grounding/bootstrap`; `POST …/grounding/build-from-project`; `POST …/grounding/memory/sync`; `POST …/grounding/memory/rebuild` |
| Deliver / publish | `POST …/worktree/accept`; `POST …/publish/manual-export`; `POST …/publish/existing-repo-pr`; `POST …/publish/new-github-repo` |

All mutations are **Tauri-origin only**. Room edits use optimistic concurrency —
read the room, mutate, PUT with the `expected_revision`; on a 409, re-read and retry.

---

## 10. Decision recipes (intent → full config)

- **"Fast and cheap."** Dev members multi-mode with a `local.*` / subscription-CLI
  pool + a mid family for escalation; reviewer a mid family; `max_parallel_workers`
  AUTO; a modest `max_model_calls`; governance `light`.
- **"Highest quality, take your time."** Strong families for dev & reviewer (multi,
  so easy tasks stay cheap); governance `light`/`strict`; checkpoints
  `per_milestone`; `block_on_problems=true`.
- **"Just build it and don't ask me."** AUTONOMOUS preset; warn about autonomy;
  offer a `max_model_calls` cap (blank = unlimited); start the run on Accept.
- **"Private / offline."** `local.*` everywhere plus a no-network runtime/tool
  policy; verify that no configured team route is remote.
- **User gave no preference.** Default: balanced team (a couple of DEVs + a
  REVIEWER on solid mid families, multi-mode), governance `light`, checkpoints
  `per_milestone`, AUTO workers, `max_model_calls=null`. A safe, runnable baseline.

---

## 11. The runnable-by-construction intake checklist (AI Wizard)

Before you may create the project, you must have — asked or reasonably assumed:

1. **What** they're building → the **North Star**.
2. **Who/why** → audience + purpose (sharpens scope).
3. **Modality** → is it a static site, a web app/API, a CLI, a desktop app, a
   binary, a container? (§5) — this is what makes it runnable.
4. **Definition of Done** that **includes a runnable check** (e.g. "opens in a
   browser and the reviewer watches it run" / "starts with one command").
5. **Entrypoint expectation** → the concrete file the team must produce
   (`index.html` / `main.py` / a `package.json` script / …).
6. **Scope / non-goals + constraints** (stack, offline, deadlines).
7. **Team + autonomy** → chosen via §2/§3 from the user's intent (or defaults).

Then present a single **"PM Changes: create this project"** review (North Star,
DoD, modality, team+models, autonomy). On Accept, create — and start the run if
the user asked you to just build it.

### What counts as a "foundation" (the concurrency clamp)

A greenfield (`new`) run is **clamped to one worker until its foundation merges to
master** — the team must scaffold a coherent base before fanning out. What
qualifies is ecosystem-aware, so the foundation task you plan first should match
the modality:

| Modality | Foundation-ready when master has |
|---|---|
| node / bundled web / compiled (go, rust, java, …) | a **build manifest** (`package.json`, `Cargo.toml`, …) **+** a source entrypoint |
| script (python, ruby, …) | **one script entrypoint** (`game.py`) — no manifest needed |
| **buildless web** (Spec 13) | an **`index.html`** whose relative `<script src>` / `<link>` graph resolves entirely against files on master, with **no bare-specifier imports / `require` / JSX** — no manifest needed |

The buildless-web row is the gravity-golf case: a game that "opens directly in a
browser with no build step" is complete on `index.html` + its relative script
modules, and must not be made to add a `package.json` it never needs. A bundled
app (bare imports, `.tsx`) still requires the manifest. If a foundation-unlocking
PR is rejected for reasons **unrelated** to the foundation it adds, the run
records a `foundation_pr_rejected_offscope` decision and escalates to you — the
clamp is held at 1, so re-scope or re-plan so the foundation can land.

### The acceptance gate (Spec 12)

A greenfield run **acquires a gate automatically** (`gate_bootstrap`, default on):
it detects and registers runtime profiles, and — when the team has authored a
runnable test on master that a one-shot **smoke run proves can execute** —
registers an `acceptance`-scoped test command. A candidate that cannot run (a
missing interpreter/dependency) is *refused* (`gate_bootstrap_refused`), because a
gate that is red forever is a wedge, not a gate. You do not need to configure test
commands for the team to have something to run.

Scope matters: an **`acceptance`** command runs on the **integrated master tree**
(the in-loop gate, dispatched between merges — `gate_min_merge_interval`, default
3 — and the delivery gate) and **never blocks a per-PR merge**; a **`unit`**
command (the default when none is declared) gates each PR as before. The latest
gate output is fed **verbatim** into subsequent dev/reviewer/tester prompts, so
"iterate until the gate passes" has a real feedback signal, and `done` requires a
green delivery gate at the delivered head.

---

## 12. What you are NOT allowed to do

- Assign a model / claim a capability the live state doesn't show (refuse instead).
- Choose `sandbox=none` (reduced isolation) without explicit user consent.
- Silently exceed the trust tier or the user's stated call cap.
- Change settings without announcing them.
- Leave a project half-configured or not runnable.

---

## Machine-readable anti-drift contract

The canary test parses this block and compares it with the real Python schemas
and FastAPI routers. Update the prose and this contract together.

> **Spec 12-18 batch, prep PR.** Seven `autonomy_defaults` keys below are landed
> ahead of their features and have **no consumers yet** — setting them changes
> nothing until the matching spec merges: `gate_bootstrap` /
> `gate_min_merge_interval` (Spec 12), `reviewer_repo_read` /
> `review_min_latency_ms` / `review_screenshot` (Spec 14), `revise_chain_limit` /
> `revise_livelock_limit` (Spec 16). They ship early so two engineers can build
> the batch in parallel without both editing `CodingAutonomyPolicy`. Each spec
> documents its own knob when it lands.

> **Spec 22-28 batch, prep PR (P0.1).** Five more `autonomy_defaults` keys below
> are landed ahead of their features and have **no consumers yet** — setting them
> changes nothing until the matching spec merges:
> `blocked_turn_limit` (Spec 25), `capability_overrides` (Spec 26).
> (`last_word_limit` is now **live** — see "Spec 23" below;
> `governance_proximity` is now **live** — see "Spec 24" below; and
> `narrow_limit` / `narrow_drain_iters` are now **live** — see "Spec 27"
> below.) Same reason as
> above: five branches build in parallel without racing `CodingAutonomyPolicy`.
> Each knob's **disable value** — `0` for the ints, `0.0` for
> `governance_proximity`, `{}` for `capability_overrides` — is required to
> reproduce today's behaviour exactly, and stays required after its spec lands.

> **F156 (G5) — `not_applicable_soft_limit` (3).** How many PRs in ONE run may merge
> on a tester `not_applicable` declaration before the run records an
> operator-visible `tests_not_applicable_over_limit` decision instead of only the
> deduped non-blocking alert. It is **not** a hard cap: a partial slice legitimately
> has no test that exercises it, and refusing the declaration would wedge the run.
> What it bounds is *invisibility* — a run leaning on the escape slice after slice is
> merging on review alone. The delivered head is still gated deterministically by the
> delivery review's full-registry run and F154's default build. Disable value: `0`.

> **F154 — `default_build_gate` (true).** When a project has **no registered test
> commands**, the delivery review derives a build/typecheck from the detected stack
> (`npm run build` → `tsc --noEmit` → `cargo build` → `go build` → `compileall`) and
> treats its failure like a failed test. Without it a greenfield project's empty
> registry reads as success at both gates and it can reach `done` with nothing ever
> compiled. Never fires when the registry is non-empty, and derives `None` (skipping
> silently) for any stack with no safe rule. Disable value: `false`.

> **SPEC-40 — the testability-contract oracle.** Four `autonomy_defaults` keys,
> all **live** and all defaulting to `true`. Unlike the batches above, each one's
> disable value is `false`:
> * `probe_adaptive_sweep` — calibrate the mechanic differential's power sweep to
>   the game's own usable range (bisect for the minimum power at which a
>   mechanic-OFF straight shot sinks) instead of anchoring it to hole geometry.
>   `false` restores the `[0.8,1.3,2.0] × dist(tee,hole)` sweep, which was 32–80×
>   miscalibrated against a game whose `shoot()` takes a speed.
> * `probe_mechanic_advisory` — keep the mechanic verdict OUT of the anchored
>   `web:probe` pass/fail, so a marginal verdict cannot drive `anchor_regressed`
>   and through it `revise_livelock`. `false` restores the old fold.
> * `probe_whitebox` — run the white-box `__probe.solution()` / `__probe.won()`
>   acceptance phase. `false` skips it entirely.
> * `probe_pr_gating` — stamp the same verdict components on the per-PR arm that
>   gate delivery, so the reviewer reviews against the real bar. `false` restores
>   the weaker per-PR verdict.

<!-- PM_REFERENCE_CONTRACT_START -->
```json
{
  "schema_version": 1,
  "provider_classes": ["anthropic", "claude_cli", "codex_cli", "cursor_cli", "custom", "google", "local", "openai"],
  "coding_roles": ["dev", "pm", "reviewer", "tester"],
  "model_modes": ["multi", "single"],
  "pm_model_modes": ["single"],
  "run_setup_fields": ["block_on_problems", "checkpoint_cadence", "checkpoint_n", "delivery_review_round_limit", "governance_mode", "grounding", "guardrail_enabled", "human_code_approval", "max_iterations", "max_model_calls", "max_parallel_workers", "max_review_rounds", "member_failure_limit", "members", "preflight_enabled", "team_room_id"],
  "autonomy_defaults": {
    "blocked_turn_limit": 3,
    "capability_overrides": {},
    "checkpoint_cadence": "per_milestone",
    "checkpoint_n": 5,
    "completion_refused_limit": 2,
    "convergence_clamp_merge_rate": 0.35,
    "convergence_clamp_ratio": 0.5,
    "convergence_release_merge_rate": 0.5,
    "convergence_release_ratio": 0.35,
    "convergence_stall_limit": 20,
    "convergence_window": 20,
    "default_build_gate": true,
    "delivery_review_round_limit": 3,
    "dev_repo_read": false,
    "diff_deadlock": true,
    "diff_stasis_epsilon": 0.12,
    "foundation_stall_limit": 12,
    "gate_bootstrap": true,
    "gate_min_merge_interval": 3,
    "gate_stall_limit": 8,
    "governance_proximity": 0.6,
    "hot_file_escalation_threshold": 4,
    "hot_file_freeze_stall_limit": 15,
    "hot_file_threshold": 2,
    "last_word_limit": 2,
    "max_iterations": 200,
    "max_model_calls": null,
    "max_parallel_workers": null,
    "member_failure_limit": 3,
    "model_escalation_limit": 2,
    "narrow_drain_iters": 5,
    "narrow_limit": 3,
    "not_applicable_soft_limit": 3,
    "plan_streak_limit": 6,
    "pm_assist_limit": 1,
    "pm_idle_limit": 2,
    "probe_adaptive_sweep": true,
    "probe_mechanic_advisory": true,
    "probe_pr_gating": true,
    "probe_whitebox": true,
    "review_min_latency_ms": 0,
    "review_screenshot": false,
    "revert_overlap": 0.7,
    "reviewer_repo_read": false,
    "revise_chain_limit": 3,
    "revise_livelock_limit": 5,
    "schema_reject_limit": 3,
    "strict_file_partition": true,
    "task_reassignment_limit": 2,
    "web_probe": true,
    "web_probe_frames": 30,
    "wedge_min_tasks": 10,
    "wedge_stall_limit": 5,
    "worker_unproductive_limit": 2
  },
  "checkpoint_cadences": ["every_n_tasks", "off", "on_merge_ready", "per_milestone"],
  "governance_modes": ["light", "off", "strict"],
  "human_code_approval": ["final_only", "none", "per_milestone", "per_slice"],
  "runtime_profile_kinds": ["api", "binary", "cli", "container", "desktop", "static", "unknown", "web"],
  "runtime_modes": ["container", "managed_local", "static"],
  "sandbox_choices": ["auto", "bwrap", "docker", "none", "seatbelt"],
  "implemented_modalities": ["binary", "cli", "container", "desktop", "server", "static"],
  "declared_unimplemented_modalities": ["emulation", "mobile"],
  "grounding_modes": ["build_from_project", "build_from_repo", "existing", "none"],
  "control_routes": [
    {"method": "POST", "path": "/coding/projects/{project_id}/run-setup/confirm"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/autonomy"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/governance/settings"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/guardrail"},
    {"method": "PUT", "path": "/council/rooms/{room_id}"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/runtime/profiles/{profile_id}"},
    {"method": "POST", "path": "/coding/projects/{project_id}/runtime/detect"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/north-star"},
    {"method": "POST", "path": "/coding/projects/{project_id}/focus"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/focus/reorder"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/focus/{focus_id}"},
    {"method": "POST", "path": "/coding/projects/{project_id}/focus/{focus_id}/accept"},
    {"method": "POST", "path": "/coding/projects/{project_id}/tasks"},
    {"method": "PATCH", "path": "/coding/projects/{project_id}/tasks/{task_id}"},
    {"method": "POST", "path": "/coding/projects/{project_id}/run"},
    {"method": "POST", "path": "/coding/projects/{project_id}/run/resume"},
    {"method": "POST", "path": "/coding/projects/{project_id}/run/continue"},
    {"method": "POST", "path": "/coding/projects/{project_id}/run/cancel"},
    {"method": "POST", "path": "/coding/projects/{project_id}/pm-ask"},
    {"method": "GET", "path": "/coding/projects/{project_id}/pm-chat"},
    {"method": "POST", "path": "/coding/projects/{project_id}/interject"},
    {"method": "PUT", "path": "/coding/projects/{project_id}/grounding/corpus-binding"},
    {"method": "POST", "path": "/coding/projects/{project_id}/grounding/bootstrap"},
    {"method": "POST", "path": "/coding/projects/{project_id}/grounding/build-from-project"},
    {"method": "POST", "path": "/coding/projects/{project_id}/grounding/memory/sync"},
    {"method": "POST", "path": "/coding/projects/{project_id}/grounding/memory/rebuild"},
    {"method": "POST", "path": "/coding/projects/{project_id}/worktree/accept"},
    {"method": "POST", "path": "/coding/projects/{project_id}/publish/manual-export"},
    {"method": "POST", "path": "/coding/projects/{project_id}/publish/existing-repo-pr"},
    {"method": "POST", "path": "/coding/projects/{project_id}/publish/new-github-repo"}
  ]
}
```
<!-- PM_REFERENCE_CONTRACT_END -->
