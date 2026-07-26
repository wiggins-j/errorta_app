# Spec 26 — Role capability closure (grant, unseat, or override)

**Source:** [`ROADMAP-autonomy.md`](ROADMAP-autonomy.md) Phase 3, gap **G4** —
"duty without capability, still"; the enforcement arm
[GL03](../../.superpowers/specs/GL03-capability-role-fit.md) §6 Item 2 states as
an invariant and
[GL05](../../.superpowers/specs/GL05-single-vs-multi-and-parallelism.md) Item 1
implements as an **advisory**.
**Target version:** v0.1 (engine — `errorta_council/coding/`; one route pair)
**Status:** proposed
**Owner:** wiggins-j

---

## Problem

> "Grant or delete" is a principle this codebase states, documents, tests, and
> does not enforce.

`docs/coding/PM_REFERENCE.md:81-88` states it as *the governing invariant*.
`capabilities.audit_grant_or_delete` (`capabilities.py:328-359`) computes it.
`topology_audit.audit_topology` (`topology_audit.py:74-112`) scores every seated
role against it. `test_gl05_parallelism.py:82-131` locks six cases of it. And
`_audit_topology_advisory` (`runner.py:2186-2228`) — the one caller, invoked once
at run setup (`runner.py:6335`) — writes a decision, raises a non-blocking
attention Alert, and returns. Nothing reads either. The run proceeds exactly as
if the audit had not run.

So the advisory fires and never resolves. That much the roadmap already says. What
reading the code adds is that it is **worse than the roadmap's summary**, in two
specific ways.

**First: it fires for two roles, not one.** `audit_grant_or_delete`'s REVIEWER arm
(`capabilities.py:351-354`) requires `cap.repo_read or cap.can_execute`.
`_repo_read_for` reads `policy.reviewer_repo_read` (`capabilities.py:62-69`), and
that field's default is **`False`** (`autonomy.py:167` — *"Defaults to
`dev_repo_read` deliberately"*, and `dev_repo_read` is default-off and locked
default-off, `autonomy.py:154-162`). `_CAN_EXECUTE` is `False` for every role by
construction (`capabilities.py:59`). So on a default-policy four-role council the
audit emits **two** advisories on every run:

> role TESTER's duty demands execution but its manifest grants no gate/executor
> capability — grant it (SPEC-12) or delete the role
>
> role REVIEWER's duty demands verification but its manifest grants no
> read/execute capability — grant it (SPEC-14) or delete the role

The second is the seat GL05's own table calls *"the 26–92% false-rejection
machine"* and *"another opinion in the same loop"*. It is seated by default, and
the system says so, out loud, once per run, to nobody.

**Second: the one capability that *could* arrive later arrives in the wrong
currency.** This is the finding that reshapes the whole spec, so it is traced in
full below.

### The TESTER's capability is measured with the wrong instrument

The audit asks `cap.gate_available` (`capabilities.py:347`). That resolves through
`gate_state.gate_available` (`gate_state.py:40-57`) to `evidence._tests_required`
(`evidence.py:118-127`):

```
gate_available == bool(store.get_test_commands())  OR  _has_runnable_runtime(store)
```

`_has_runnable_runtime` is *any registered runtime profile with a `start` argv*
(`evidence.py:53-62`). Now trace a greenfield run:

1. **At run setup**, master is empty. `gate_bootstrap.maybe_bootstrap`
   (`runner.py:6328` → `gate_bootstrap.py:171-185`) runs first. `_bootstrap_runtime`
   calls `runtime.detect`, which needs an `index.html` for the static profile
   (`runtime.py:1051-1053`) and a manifest/entrypoint for every other detector —
   an empty tree yields nothing. `_bootstrap_acceptance_command` needs a
   `*.test.js` or `tests/*.py` on master (`gate_bootstrap.py:107-139`) — nothing.
   So `gate_available` is `False`, and `_audit_topology_advisory` fires seven
   lines later (`runner.py:6335`). **Correct verdict, no consequence.**
2. **After the first merge that lands an `index.html`**, `_arm_gate_after_merge`
   calls `maybe_bootstrap` again (`runner.py:3979-3983`), `_detect_static` now
   fires, and a `managed_local` profile with `start: ["python", "-m",
   "http.server", …]` is registered (`runtime.py:1064-1070`). `_has_runnable_runtime`
   flips `True`. `gate_available` flips `True`. `audit_grant_or_delete` would now
   say the TESTER is **capable**.
3. **And the TESTER still cannot be dispatched.** Its turn chooses from
   `store.get_unit_test_commands()` (`runner.py:3206`, `:5514`), and its task is
   only ever created when that registry is non-empty (`runner.py:5481-5483`).
   `get_unit_test_commands` filters to `scope == "unit"` (`ledger.py:1335-1342`),
   and `gate_bootstrap` registers **acceptance scope only**, deliberately
   (`gate_bootstrap.py:26-28`, `:125`, `:136`). A runtime profile is a *launch
   probe for the engine*, not a command for the tester.

So re-running the audit unchanged would resolve the TESTER advisory on a signal
that has nothing to do with the TESTER. That is a **false resolve** — strictly
worse than never resolving, because it launders an unclosed gap into a green
check.

And the closing fact: the only production writers of the test-command registry
are `gate_bootstrap` (acceptance only) and `PUT /coding/projects/{id}/test-commands`
(`routes/coding.py:3161-3175`, reached from the app UI and `errorta test-commands
set`, `errorta_cli/commands/testcfg.py:64`). **No engine path can ever register a
unit-scoped command.** A headless autonomous run therefore cannot arm its own
TESTER at all — not on iteration 1, not on iteration 200. The seat is not
"waiting for a gate". It is structurally empty for the entire class of run this
product exists to drive.

That is the honest answer to *"trace whether the runtime-profile arm flips the
TESTER's manifest to capable"*: **it flips the manifest, and the manifest is
lying.**

## Why the existing machinery didn't catch it

Every piece is present and pointed one notch away from enforcement.

- **The audit is pure, tested, and composable** (`topology_audit.py` imports only
  `.capabilities`/`.topology`; `audit_topology` calls `audit_grant_or_delete`
  scoped to one role, `:105`, so the message text lives in one place). It was
  built to be wired to a decision. Its own docstring records the choice:
  *"advisory, not a hard blocker on run start (the task's ruling over the spec's
  stricter 'refuse at run-setup')"* (`topology_audit.py:19-22`). GL05 Item 1's
  acceptance criterion says the opposite — *"refused at run-setup"* — so the
  spec and the code disagree, and the code shipped.
- **The consequence surface already exists, for a different check.**
  `member_health_preflight_failed` (`routes/coding.py:2426-2435`) refuses a run
  start with a structured 409 and an `unhealthy` list; `POST
  /run-setup/preflight` (`:2838-2855`) is its non-blocking preview. That is
  exactly the warn-then-act shape a capability verdict needs, and nothing
  capability-shaped is routed through it.
- **Unseating is already the loop's native semantics.** `decide_next` skips any
  role with no seated members (`topology.py:301-304`); `plan_next_batch` does the
  same; `_has_open_work` only counts a `_WORKER_PRIORITY` role that is in the
  seated set (`topology.py:191-197`). An unseated role costs zero dispatches and
  zero model calls, with no new machinery. Nobody ever unseated one.
- **The signal has a resolution idiom, unused here.**
  `attention.resolve_stale_member_health` dismisses an open Problem the current
  roster has fixed, via `_write_resolution(..., state="dismissed",
  action="dismiss", by="system")` (`attention.py:582-619`, the write at `:617`);
  `resolve_stale_worker_unproductive` (`:622-704`) does the same for exclusions.
  The topology advisory raises with `attention.raise_signal`
  (`runner.py:2221-2224`) and has no counterpart call anywhere.
- **And the audit fails open, totally.** `_audit_topology_advisory`'s body is one
  `try:` with a bare `except Exception: pass` (`runner.py:2196`, `:2227-2228`).
  Correct for an advisory; unacceptable for anything binding, and the reason
  Item 1 splits the evaluation from the guarded reporting.

## Goals

- **One closure verdict per seated role**, computed from the manifest, with
  exactly three outcomes — `capable`, `deferred`, `unclosable` — replacing a
  boolean that cannot express the greenfield TESTER.
- **A consequence at seat time.** For every seated role, `duty ⊆ capability`, or
  the role is not seated, or an override is recorded on the ledger. No fourth
  state, and no state whose only artifact is a decision row.
- **A deferred role really resolves**, on the capability its duty actually needs
  — and the advisory that has fired on every run since GL05 landed goes to
  `dismissed`, once, when it does.
- **The predicate the audit reads is the predicate the loop obeys.** One
  definition of "this TESTER can be dispatched", read by the audit, the task
  spawn (`runner.py:5481`), and the merge gate (`runner.py:4347-4348`).
- **A drift lock**: a role added to the manifest without a duty→capability entry
  fails the build.

## Non-goals

- **Not a permission system.** Enforcement stays fail-closed in
  `turn_controller.execute_dev_turn` (`turn_controller.py:161-187`) and the CLI
  allowlist; `_ROLE_TOOLS` (`:27-32`) is untouched. This spec decides *who sits
  down*, never *what a seated role may call*. (GL03 §5, first bullet, verbatim.)
- **Not removing a role from the product.** `TESTER` and `REVIEWER` remain
  first-class in `topology.py:26-29`, the team builder (`errorta team add
  --tester`), and every prompt. "Not seated" is per-run and reversible within the
  run (Item 3).
- **Not granting execute tools to DEV or REVIEWER.** Spec 12's non-goal stands:
  execution is engine-driven, `_CAN_EXECUTE` is `False` for every role by design
  (`capabilities.py:56-59`), and nothing here changes it.
- **Not flipping `reviewer_repo_read` / `dev_repo_read` to `True`.** That default
  is stated in exactly one place with a paragraph of reasoning and a drift lock
  (`autonomy.py:154-167`, `test_spec12_18_prep.py`); flipping it is a live
  behaviour change for every reviewer turn and belongs in its own PR. This spec
  is written to be *correct under the shipped defaults*, which is precisely why
  it cannot refuse a run (Item 2).
- **Not adding a unit-scoped arm to `gate_bootstrap`.** Named as the real fix for
  the empty-TESTER case in Out of scope, with the reason it is not taken here.
- **Not a new detector or stop reason.** SPEC-27 owns the detector family.

---

## Item 1 — The closure verdict: three outcomes, one computation

**Design.** `audit_grant_or_delete` returns `list[str]` — a message or nothing.
That shape cannot distinguish *"this REVIEWER will never be grounded without an
operator edit"* from *"this TESTER has no gate **yet**"*, and the distinction is
the whole spec. Add, in `capabilities.py` beside it (same module, same import
discipline — `.topology` / `.turn_controller` / `.gate_state`, never `runner`):

```python
@dataclass(frozen=True)
class ClosureVerdict:
    role: str
    outcome: str        # "capable" | "deferred" | "unclosable"
    capability: str     # "execution" | "verification" | "authoring" | ""
    reason: str         # one legible line — the audit_grant_or_delete text, verbatim
    remedy: str         # the one action that closes it

def role_closure(manifest: dict[str, RoleCapability], *,
                 seated_roles: tuple[str, ...],
                 overrides: frozenset[str] = frozenset()) -> list[ClosureVerdict]
```

`role_closure` **composes** `audit_grant_or_delete` rather than reimplementing it
— same call shape `topology_audit.audit_topology` already uses (`:105`), so the
enforcement text keeps living in one place — and then classifies each violation:

| Role | Duty | Capability read | Outcome when absent | Why |
|---|---|---|---|---|
| PM | coordination | — | always `capable` | category (a); GL05 `_BASELINE_CATEGORY` (`topology_audit.py:49-52`) |
| DEV | authoring | `cap.tools` | `unclosable` | `_ROLE_TOOLS[DEV] == ("code_write",)` is a constant (`turn_controller.py:29`); if it is ever empty, waiting cannot help |
| REVIEWER | verification | `cap.repo_read or cap.can_execute` | `unclosable` | `policy.reviewer_repo_read` is a persisted policy field (`autonomy.py:167`), read once per manifest (`capabilities.py:68`); no run event changes it |
| TESTER | execution | **`cap.can_dispatch`** (new, Item 3) | `deferred` | the unit-command registry is writable mid-run (`routes/coding.py:3161`), so the gap can close without restarting |

The `deferred` / `unclosable` split is not a severity judgement. It is a
statement about **who can still act**: a `deferred` gap is one the *run* can
close, an `unclosable` gap is one only the *operator* can close. That is the only
axis that matters for choosing a consequence, and it is decidable from the code
that produces each flag.

**The override.** One field on the persisted run config
(`store.set_run_config`, written by `confirm_run_setup` at `routes/coding.py:2924`):

```json
"capability_overrides": ["tester"]
```

set from the CLI by `errorta team seat-anyway <role>` (writing the local draft
that `team apply` pushes, `errorta_cli/teamdraft.py`) and from the desktop gate.
An overridden role is **seated regardless of its verdict**, and the verdict is
still computed, still recorded as a decision, and still rendered — an override
suppresses the *consequence*, never the *finding*. This is the operator who wants
an idle TESTER on the board (Edge cases), and it is the escape hatch that keeps
this spec from becoming the fifth unsatisfiable constraint the roadmap's G1 warns
about (`ROADMAP-autonomy.md:56-59`).

**Δ note — why a new function rather than widening `audit_grant_or_delete`.**
`audit_grant_or_delete`'s `list[str]` return is consumed by
`topology_audit.audit_topology` (`:105-108`) and asserted on in five places in
`test_gl03_capability_alarm.py` (`:126-153`). Widening it churns both for no gain:
the classification is *new information layered on top of* the violation, not a
change to what counts as a violation. Composition keeps GL03's message text as
the single source and keeps its tests meaningful.

**Acceptance.** On a default four-role council with an empty master,
`role_closure` returns `capable` for PM/DEV, `unclosable` for REVIEWER
(`reviewer_repo_read=False`), `deferred` for TESTER. Flipping
`reviewer_repo_read` to `True` moves REVIEWER to `capable` with no other change.
`seated_roles=(PM, DEV)` returns two `capable` verdicts and nothing else. An
overridden role's verdict is unchanged — only its consequence is.

## Item 2 — The consequence: seat, unseat, or override. Never refuse.

**This is the load-bearing decision, and it is the one the task asks to be
argued.** The failure surface is: **the run starts, and the un-capable role is
not seated.** It is not a refusal to start.

**Where it hooks.** `CodingRunner.run`, at the exact three lines that today build
the roster and call the advisory (`runner.py:6331-6335`):

```python
by_role = members_by_coding_role(self.members)
member_pairs = [(m["id"], coding_role_of(m)) for m in self.members
                if m.get("enabled", True)]
_audit_topology_advisory(self.store, member_pairs, policy)   # <- replaced
```

`_audit_topology_advisory` becomes `_apply_role_closure(store, member_pairs,
by_role, policy)`, which computes `role_closure` over the seated set, **removes
every `unclosable`/`deferred` non-overridden role from both `member_pairs` and
`by_role`**, and records/publishes the verdicts (Item 4). `member_pairs` is what
`run_coding_loop` schedules from (`runner.py:6350`, `autonomy.py:876`); `by_role`
is what `build_run_turn` resolves a member from (`runner.py:6342-6348`). Filtering
one and not the other would seat a ghost.

### Why not refuse to start

1. **A refusal is unsatisfiable on the shipped defaults.** As traced in Problem,
   a default four-role council on a fresh project flags **both** TESTER
   (`gate_available=False`) and REVIEWER (`reviewer_repo_read=False`, the locked
   default at `autonomy.py:167`). A binding refusal would refuse the product's
   own default configuration on every new project — and the operator's only
   in-band fixes would be to edit `autonomy.json` and to unseat the TESTER, on
   every project, forever. Shipping that is shipping the fifth
   unsatisfiable-constraint bug inside the spec written to close G4.
2. **Unseating is free and already correct.** `decide_next` skips a role with no
   members (`topology.py:301-304`); `plan_next_batch` mirrors it;
   `_has_open_work` (`topology.py:191-197`) only counts roles in the seated set,
   so an unseated role cannot keep a finished project alive either. There is no
   new enforcement path to write and no new way to wedge.
3. **The refusal precedent does not transfer.** `member_health_preflight_failed`
   (`routes/coding.py:2426-2435`) may refuse because a logged-out provider is
   unsatisfiable *by any amount of running* — no run event logs a provider back
   in. A missing gate is the opposite: it is exactly the thing a run produces
   (`_arm_gate_after_merge` → `maybe_bootstrap`, `runner.py:3979-3983`). Refusing
   to start because the run has not yet produced what starting produces is a
   deadlock with a message attached.
4. **Refusal also cannot express the override cheaply.** A 409 forces a
   round-trip and a client that knows how to retry with a flag; unseating plus a
   recorded, visible verdict gives the operator the same choice with the run
   already moving.

The one case that **does** refuse, stated so the boundary is a decision rather
than an omission: an `unclosable` verdict on **DEV**. `audit_grant_or_delete`'s
DEV arm (`capabilities.py:355-358`) can only fire if `_ROLE_TOOLS[DEV]` is
emptied, and unseating the DEV leaves a council that cannot produce work at all —
`decide_next` would fall through to `Plan` forever (`topology.py:331-334`).
That refuses at run start with a structured `role_capability_unclosed` 409 in the
`member_health_preflight_failed` shape. It is unreachable today and it is a
regression tripwire, not a feature.

### Why not "just a log line" — the coupling that makes unseating safe

Unseating a TESTER is **not** safe today, and this is the half the advisory could
never have delivered. Verified:

- `runner.py:5481-5483` spawns `test PR: <branch>` with `role=TESTER` whenever
  `store.get_unit_test_commands()` is non-empty. It does **not** check that a
  TESTER is seated.
- `_set_mergeable_if_ready` (`runner.py:4332-4360`) then holds the PR: `tests_ok`
  is `p["tests_passed"] is True or not store.get_unit_test_commands()`
  (`:4347-4348`).

So on a project with a unit command and no seated TESTER, every reviewer-approved
PR spawns a task nobody can take and sits un-mergeable until `dispatch_wedged`.
Trading an unread advisory for a wedge is not a fix.

**Design.** Both sites, plus the audit, read one predicate:

```python
# capabilities.py — the single definition of "a TESTER can actually be dispatched"
def tester_dispatchable(store) -> bool:
    return bool(store.get_unit_test_commands())
```

surfaced on `RoleCapability` as `can_dispatch: bool` (`capabilities.py:37-53`,
`to_dict` at `:48-53`), populated in `capability_manifest` (`:92-111`) alongside
`gate_available`. `audit_grant_or_delete`'s TESTER arm reads `can_dispatch`
instead of `gate_available` (`:347`). `runner.py:5481` gains `and
_tester_seated(store)`; `_set_mergeable_if_ready`'s `tests_ok` gains the same
disjunct, so an unseated TESTER makes the tests-green gate vacuously satisfied by
exactly the mechanism the existing comment already describes for a project with
no commands (`runner.py:4336-4342`).

**`gate_available` is not changed.** It remains *"is there an acceptance gate that
CAN produce evidence?"* (`capabilities.py:46`), which is what SPEC-15's PM segment
(`capabilities.py:114-132`), SPEC-17's tool catalog (`turn_controller.py:69-122`),
and GL03's confabulation detector (`capabilities.py:296-303`) all correctly want.
The bug was never `gate_available`'s definition; it was the audit reading the
engine's capability and calling it the tester's.

**Acceptance.** A council with a `deferred` TESTER runs with `member_pairs`
carrying PM/DEV/REVIEWER only; no `test PR:` task is created; an approved PR
reaches `mergeable` on review alone. With `capability_overrides: ["tester"]` the
TESTER is seated, the verdict is still `deferred`, and the decision row says so.
A `run_setup` with `reviewer_repo_read=True` seats all four unchanged.

## Item 3 — Deferred capability: re-evaluation, re-seating, and resolution

**Design.** A `deferred` verdict is a claim about *now*, so it is re-evaluated
whenever the inputs can have changed. There are exactly two such moments, and one
already exists as a function:

- **`_arm_gate_after_merge`** (`runner.py:3965-4002`) — called after every merge
  that advances master, and already the place `maybe_bootstrap` re-attempts gate
  acquisition (`:3979-3983`). Re-run `role_closure` immediately after that call,
  on the same guarded path.
- **Run setup** — the initial evaluation (Item 2), which also covers the operator
  registering a command between runs via `PUT /test-commands`
  (`routes/coding.py:3161`).

A mid-run `PUT /test-commands` on a *live* run is picked up at the next merge, not
instantly. Stated as a bound, not hidden: the loop has no config-watch seam, the
merge point is the only quiescent moment the runner already re-derives gate state
at, and adding a second poll for a rare operator action is not worth a new
iteration hook.

**Re-seating.** `member_pairs` is a plain `list` passed by reference into
`run_coding_loop` (`autonomy.py:876`) and read fresh every iteration by
`decide_next` (`autonomy.py:1810`, `:2181`), `plan_next_batch` via `_idle_members`
(`:2011-2013`, `:2103-2104`), and `runtime_cap` (`:2040`, `:2065`). The sequential
and concurrent loops hand the **same list object** back and forth
(`autonomy.py:1802-1805`, `:2068-2071`). So re-seating is an `append` to that
list, and it takes effect on the next iteration with **no signature change** to
the `RunTurn` seam (`autonomy.py:837`) — the constraint SPEC-24 Item 1's Δ note
spends three paragraphs establishing.

**Δ note — the one hazard, and its one-line fix.** `_run_concurrent_loop` sizes
its pool **once**, before the loop: `ThreadPoolExecutor(max_workers=
effective_parallelism(policy, members) + 2)` (`autonomy.py:2044`), and
`effective_parallelism` counts non-PM members (`:509-516`). Appending a re-seated
worker after that could raise `runtime_cap` above the pool size, silently
serializing dispatch behind a full pool. Fix: size the pool from the **full
enabled roster** (pre-closure), which `CodingRunner` already has and can pass, or
equivalently from `effective_parallelism` over the unfiltered pairs. The pool is
an upper bound only (the comment at `:2041-2043` says so), so widening it is
strictly safe and changes nothing for a run with no deferred roles.

**Resolution — the half that has never happened.** When a `deferred` role becomes
`capable`, in the same guarded block:

1. **Dismiss the open advisory.** Find the open signal with `source ==
   "topology_audit"` and a matching `context["role"]` (`attention.list_open`,
   `attention.py:188-189`) and write `state="dismissed", action="dismiss",
   by="system"` through `_write_resolution` — the exact idiom
   `resolve_stale_member_health` uses (`attention.py:617`) and the reason that
   function exists: *"a stale, blocking Problem keeps gating the next run"*
   (`:594-596`). The signal is an Alert here, not a Problem, so nothing was
   gated — but a permanently-open Alert is how an operator learns to ignore the
   attention list, which is its own failure.
2. **Record `choice="role_capability_closed"`** naming the role, the capability,
   and what closed it (the registered command id). The existing
   `topology_advisory` decision row (`runner.py:2212-2214`) is left verbatim —
   the ledger is append-only and the pair *(advisory at iteration 0, closed at
   iteration N)* is the trace that proves the loop works.
3. **Re-seat**, as above.

The `context` dict on the raised signal must therefore carry `{"role": …,
"capability": …, "outcome": …}` rather than today's `{"advisory": msg}`
(`runner.py:2224`) — a string-prefix dedupe (`title[:80]`, `:2209`) cannot key a
resolution.

**Acceptance.** A run that starts with a `deferred` TESTER, then has a
unit-scoped command registered and a merge land, ends with: the TESTER in
`member_pairs`, the `topology_audit` signal in state `dismissed`, one
`role_capability_closed` decision, and the original `topology_advisory` decision
untouched. A run where the gate never arrives ends with the signal still open —
which is now *true*, and is criterion #4 of the roadmap satisfied in the
negative. A registered **acceptance**-scoped command (the only kind
`gate_bootstrap` can produce, `gate_bootstrap.py:125`, `:136`) does **not**
resolve the TESTER — the false-resolve regression lock.

## Item 4 — Where the verdict shows up

Three surfaces, none of them new machinery.

**The operator, before they commit.** `POST /run-setup/preflight`
(`routes/coding.py:2838-2855`) returns `{"unhealthy": [...]}` today; it gains
`"capability": [...]` — one entry per non-`capable` verdict with its `role`,
`outcome`, `reason`, and `remedy`. Non-blocking, exactly as the member-health
probe is non-blocking there while `/run` is the one that refuses
(`:2426-2435`). The CLI already routes `errorta setup --preflight` and `errorta
team preflight` through it (`errorta_cli/commands/runctl.py:132-136`,
`commands/team.py:266`) and renders the result via `_rr.render_preflight`, so
this is one renderer addition. `GET /run-setup` (`:2813-2835`) carries the same
list so the readiness view (`errorta_cli/render/runctl.py:137`) shows it without
a probe.

The remedy strings are the actionable half and are the reason the verdict carries
one: *"grant it — set `reviewer_repo_read: true` in the project's autonomy
policy (SPEC-14)"*, *"unseat it — `errorta team disable tester`"*, *"seat it
anyway — `errorta team seat-anyway tester`"*.

**The PM, during the run.** Cross-reference, do not duplicate:
[SPEC-24](SPEC-24-governance-visibility.md) Item 1 establishes
`run_state.detector_state` as *the* seam for run-level state the PM must see, and
Item 2's table is the registry of rows. This spec publishes its verdicts under
`run_state.role_capability` at the same quiescent points and adds **one row** to
SPEC-24's snapshot — `{"detector": "role_capability", "reading": "TESTER seat
deferred: no unit-scoped test command registered"}` — rendered by SPEC-24's
renderer under SPEC-24's framing rules (observed state, no imperative, Item 4
there). No second renderer, no second prompt segment, no wording rules restated
here.

Ordering note: if SPEC-26 lands first, the verdicts sit on `run_state` with the
operator surfaces above as their only readers, and SPEC-24 picks up the row when
it lands. Neither spec blocks the other; the seam is a JSON key.

Why the PM must see it at all: SPEC-15's `pm_capability_segment`
(`capabilities.py:114-132`) already tells the PM what each role *can* do, and it
reads the same manifest — so without this row the PM would be told the TESTER
"runs the registered test commands via the engine gate" (`:85-86`) about a role
that is not in the room. Two statements, two values, in the same prompt. That is
the [SPEC-19](SPEC-19-version-identity-and-build-provenance.md) failure shape,
and the fix is the same: derive both from one manifest, and make the seating
verdict part of what is derived.

**The ledger.** Unchanged in kind from today: one deduped decision per verdict.
`choice="topology_advisory"` is kept for the flagged case (so existing dedupe and
any operator tooling keep working) and joined by `role_capability_seated` when an
override forces a seat, and `role_capability_closed` on resolution.

**Acceptance.** `errorta setup --preflight` on a default four-role greenfield
council prints two capability findings with remedies and exits 0. `GET
/run-setup` carries the same two. `run_state.role_capability` carries all four
verdicts. No prompt bytes change from this spec alone (the PM row is SPEC-24's
renderer).

## Item 5 — The invariant, and the lock that keeps it true

**The invariant**, stated so it can be tested rather than admired:

> For every role seated in a run, the manifest grants a capability sufficient for
> that role's duty — or the role is not seated, or a `capability_overrides` entry
> naming it is recorded on the run config. There is no fourth state.

**The lock.** A data-driven test in the `test_spec12_18_prep.py` /
`test_every_engine_stop_reason_is_triaged` style
(`python/tests/cli/test_runctl_mutations.py:547`), asserting:

1. **Total coverage.** Every key `capability_manifest` produces
   (`capabilities.py:104` — the roles tuple is inlined there *and* at `:120` *and*
   as `teamdraft.CODING_ROLES`, `errorta_cli/teamdraft.py:41`, which is itself a
   small instance of the SPEC-19 multiple-declarations shape) has an entry in
   `role_closure`'s duty→capability table. Adding a fifth role without a
   capability story fails here — **this is the requirement's "a new role cannot
   be added without a capability story"**, and it is the only place in the tree
   that would catch it.
2. **Closure at run start.** Given any manifest and any seated set, after
   `_apply_role_closure` every role remaining in `member_pairs` is either
   `capable` or named in `capability_overrides`. Property-style over the 2⁴ seated
   subsets × the capability flag combinations, which is small enough to enumerate.
3. **The three outcomes are stable.** REVIEWER-without-read is `unclosable`,
   TESTER-without-`can_dispatch` is `deferred`, DEV-without-tools is
   `unclosable`, PM is always `capable` — a truth table, in the style
   `test_gl05_parallelism.py:82-131` already uses for the advisory it replaces.

**Acceptance.** The lock is green on the committed tree. Emptying
`_ROLE_TOOLS[TESTER]`'s hypothetical grant, adding a role to
`capability_manifest`'s loop, or renaming a duty flag each turn it red with a
message naming the role and the missing table entry.

---

## Implementation notes

- **`python/errorta_council/coding/capabilities.py`** — `ClosureVerdict`,
  `role_closure`, `tester_dispatchable`; `can_dispatch` on `RoleCapability`
  (`:37-53`) and in `capability_manifest` (`:92-111`); `audit_grant_or_delete`'s
  TESTER arm (`:347`) reads `can_dispatch`. Import discipline unchanged
  (`:23-24`). `gate_available`, `pm_capability_segment`, `classify_task_text`,
  and `confabulation_from_failure` are untouched.
- **`python/errorta_council/coding/topology_audit.py`** — unchanged. It remains
  the pure role-justification scorer; `role_closure` consumes
  `audit_grant_or_delete` directly, as `audit_topology` does (`:105`). The
  module's docstring paragraph declaring itself advisory (`:19-22`) is corrected
  to point at this spec.
- **`python/errorta_council/coding/runner.py`** — `_audit_topology_advisory`
  (`:2186-2228`) becomes `_apply_role_closure`, returning the filtered roster;
  call site `:6335` consumes the return and rebinds `member_pairs`/`by_role`
  before `build_run_turn` (`:6342`). Re-evaluation + resolution in
  `_arm_gate_after_merge` after `:3983`. `_tester_seated` disjunct at `:5481` and
  in `_set_mergeable_if_ready`'s `tests_ok` (`:4347-4348`). The signal's
  `context` gains `role`/`capability`/`outcome` (`:2224`).
  **Guard discipline:** the *evaluation* is pure and must not be swallowed; the
  *reporting and resolution* keep today's blanket guard (`:2227-2228`). A ledger
  hiccup must not silently seat an un-capable role, and must not fail a run
  either — so an evaluation failure seats the full roster and records
  `role_capability_indeterminate`. Fail-open on the roster, never silent.
- **`python/errorta_council/coding/autonomy.py`** — one line: pool sizing at
  `:2044` takes the full roster. No detector, threshold, or loop-shape change.
- **`python/errorta_council/coding/attention.py`** — one function beside
  `resolve_stale_member_health` (`:582`): `resolve_closed_capability(project_id,
  role, *, store)`, dismissing the matching open `topology_audit` signal through
  `_write_resolution` (`:220`).
- **`python/errorta_app/routes/coding.py`** — `capability` in the
  `/run-setup/preflight` response (`:2838-2855`) and the `GET /run-setup` payload
  (`:2813-2835`); `capability_overrides` accepted on `_RunSetupConfirmBody`
  (`:2780-2800`) and persisted with the team at `:2924`. The `/run` refusal
  (Item 2, DEV-only) beside `member_health_preflight_failed` (`:2426-2435`).
- **`python/errorta_cli/`** — `teamdraft` carries `capability_overrides`;
  `team seat-anyway <role>` beside `enable|disable` (`commands/team.py:293-295`);
  `render/runctl.py` renders the capability findings in `render_preflight` /
  `render_setup` (`:137`).
- **`docs/coding/PM_REFERENCE.md`** — the grant-or-delete section (`:68-95`) is
  amended from *"it pages the PM, it never blocks the run"* to the three
  outcomes. Note the F145 canary (`python/tests/coding/test_f145_pm_reference.py`)
  asserts the embedded policy JSON only, so prose edits are free — but if a policy
  field is ever added here, it must land in the same PR.
- **No new stop reason, no schema version bump, no `_ROLE_TOOLS` change, no
  prompt-string change.**

## Edge cases

- **A single-DEV council (PM + DEV).** Always fully seated. PM and DEV are
  `capable` by construction (`topology_audit.py:49-52`,
  `_ROLE_TOOLS[DEV]` non-empty), and this is the single-agent-plus-coordination
  baseline the entire RQ5 principle defends (GL05 §Design principle;
  `test_gl05_parallelism.py:118-123`). This spec must never make a PM+DEV run
  ask the operator anything.
- **An operator who WANTS an idle TESTER.** `capability_overrides` seats it. The
  cost is honest and bounded: `decide_next` only assigns a role a task from
  `ledger.next_tasks(role, …)` (`topology.py:312`), and with no unit commands no
  `test PR:` task is ever created (`runner.py:5481`), so an overridden idle TESTER
  consumes a roster slot and one `effective_parallelism` count — not model calls.
  The `role_capability_seated` decision is what answers the later question *"why
  did my tester never do anything?"* with something other than silence.
- **The gate appears mid-run.** Item 3. Note the asymmetry: only a **unit-scoped**
  registration re-seats the TESTER. A `gate_bootstrap` acceptance registration
  (`gate_bootstrap.py:252`) or a detected runtime profile (`:201-202`) advances
  `gate_available` and the in-loop gate — real progress, correctly reflected in
  the PM's capability segment — and leaves the TESTER deferred. Two different
  facts, no longer conflated.
- **GL03's confabulation alarm is the runtime symptom of the same disease.** A
  role invents `run_tests` because its manifest lacks execution
  (`capabilities.py:296-303`) — which is exactly an unclosed closure verdict,
  observed one turn later instead of at seat time. They must not double-report:
  `raise_capability_gap_alert` (`attention.py:804`) dedupes on `(source,
  role, capability)`; the closure signal for the same `(role, capability)`
  suppresses it, and a closure resolution (Item 3) dismisses both. This mirrors
  the dedupe [SPEC-25](SPEC-25-expressibility-and-negotiation.md) Item 2 specifies
  between a *typed* capability ask and a confabulation for the same pair — three
  producers, one alert. Under this spec a *seated* role should essentially never
  produce an execution-class gap, so a GL03 execution alert on a seated role
  becomes a signal that the closure table is wrong, and is worth reading as one.
- **Resume and continue.** `_start_run` recovers members from `run_config` when
  the body is empty (`routes/coding.py:2391-2397`) and is exempt from the
  `run_setup_required` gate (`:2374`). Closure is therefore evaluated in
  `CodingRunner.run` — which resume and continue both go through — not in the
  confirm route, so a resumed run re-derives its verdicts against *current* state
  and a role deferred in the previous run may be seated immediately in this one.
  `capability_overrides` travel with the persisted run config, so they survive
  resume without re-confirmation.
- **An unmarked member.** `coding_role_of` defaults to `DEV`
  (`topology.py:646-651`), so an unmarked member is always audited under a role
  that has a table entry. There is no unaudited seat.
- **Two REVIEWERs on different vendors.** `_repo_read_for` reads a **policy**
  flag, not a per-member one (`capabilities.py:62-69`), even though GL03 §7
  explicitly describes read as per-member. So the manifest cannot express "one
  grounded reviewer, one not", and this spec seats or unseats the *role*, not the
  member. Named as a known limit; a per-member manifest is a follow-up, not a
  quiet assumption.
- **`max_parallel_workers` set explicitly.** `effective_parallelism` honors an
  explicit int as a hard cap regardless of roster size (`autonomy.py:513-516`), so
  unseating a role does not lower the cap and re-seating does not raise it. Only
  the AUTO (`None`) path is roster-derived, which is the pool-sizing note in
  Item 3.
- **A ledger failure during evaluation.** Seats the full roster, records
  `role_capability_indeterminate`, run proceeds. Stated in Implementation notes;
  repeated here because "fails open to today's behaviour" is the correct answer
  and the temptation is to fail closed on a check whose whole purpose is to
  refuse things.

## Testing

- **The default-council truth (the test that would have caught this).** A default
  policy (`CodingAutonomyPolicy()`) + an empty master + a four-role roster:
  `role_closure` returns `deferred` TESTER **and** `unclosable` REVIEWER, and
  `member_pairs` after `_apply_role_closure` is exactly `[pm, dev]`. This asserts
  the Problem section's central claim against live defaults, so a future default
  flip shows up here first.
- **The false-resolve lock (the most important one).** Register an
  **acceptance**-scoped command, land a merge, re-evaluate: the TESTER stays
  `deferred` and the signal stays open. Then register a **unit**-scoped command:
  it becomes `capable`, is appended to `member_pairs`, the signal goes
  `dismissed`, and one `role_capability_closed` decision exists. Same fixture,
  two registrations, opposite outcomes — that pair is the spec.
- **The runtime-profile lock.** A registered `managed_local` static profile
  (`runtime.py:1064-1070`) flips `gate_available` to `True` and leaves
  `can_dispatch` `False`; the PM capability segment still describes the gate as
  configured; the TESTER stays deferred. This is the exact conflation the audit
  shipped with.
- **The wedge regression (Item 2's coupling).** A project with a unit command and
  a TESTER unseated by closure: a reviewer-approved PR reaches `mergeable`, and no
  `test PR:` task is ever created. Without the `runner.py:5481` / `:4347-4348`
  coupling this test hangs the PR — which is the failure mode, asserted rather
  than described.
- **Re-seating takes effect.** Append a re-seated TESTER mid-run and assert the
  next `decide_next` can return an `Assign` for it (`autonomy.py:1810`), and that
  `_run_concurrent_loop`'s pool was sized for it (`:2044`) — a spy on
  `ThreadPoolExecutor`'s `max_workers`, since a too-small pool degrades silently
  rather than raising.
- **Override.** `capability_overrides: ["tester"]` seats an un-capable TESTER;
  the verdict is still `deferred`; a `role_capability_seated` decision exists; no
  refusal anywhere.
- **PM+DEV never asks anything.** No capability finding on preflight, no
  `run_state.role_capability` non-`capable` row, byte-identical behaviour.
- **The invariant lock** (Item 5, three assertions).
- **Dedupe with GL03.** A closure signal for `(tester, execution)` plus a GL03
  confabulation for the same pair raises **one** open Alert; resolution dismisses
  it once (mirrors GL03's 352-storm dedupe test).
- **Fail-open.** A store that raises inside evaluation seats the full roster,
  records `role_capability_indeterminate`, and the run completes.
- **Regressions:** `test_gl05_parallelism.py:82-131` (the advisory truth table —
  the *pure* audit is unchanged), `test_gl03_capability_alarm.py:126-153`
  (`audit_grant_or_delete`'s return shape), `turn_controller`'s fail-closed
  rejection (`:161-187`), `test_prompt_segments_golden.py` (no prompt bytes change
  from this spec). Full coding suite + `ruff`.

## Documentation

- **`docs/coding/PM_REFERENCE.md`** — replace *"it pages the PM, it never blocks
  the run"* (`:94-95`) with the three outcomes and the override, and state the
  TESTER fact plainly: a headless run cannot arm its own tester today, so the
  seat is deferred until a unit-scoped command is registered.
- **`docs/CLI.md`** — `errorta setup --preflight` reports capability findings
  with remedies; `errorta team seat-anyway <role>`; a deferred role appears in
  `errorta decisions` and resolves in `errorta attention` when its capability
  arrives.
- **`ROADMAP-autonomy.md`** — mark SPEC-26 specified. Criterion #4 (*"the
  topology advisory either resolves or the role is not seated"*) becomes
  mechanically checkable, and its honest reading is recorded: on a headless
  greenfield run the answer is *not seated*, and that is the correct answer until
  `gate_bootstrap` can produce a unit-scoped command.

## Out of scope / follow-ups

- **A unit-scoped arm for `gate_bootstrap`** — the real fix for the empty TESTER,
  and the largest single lever on this gap. Not taken here because Spec 12 chose
  acceptance-only for a stated reason (*"a whole-project acceptance script fails
  by construction on a single-module branch"*, `gate_bootstrap.py:26-28`), and
  proposing a per-branch unit command needs its own smoke-run safeguard design
  (`gate_bootstrap.py:142-168`). Naming it here so the next reader knows the
  TESTER's deferral has a cure and what the cure costs.
- **Flipping `reviewer_repo_read` to `True`** — the one action that moves the
  REVIEWER from `unclosable` to `capable` on the default council. Its own PR, per
  `autonomy.py:154-167`.
- **A per-member (rather than per-role) capability manifest** — GL03 §7 assumes
  one; `capabilities.py:62-69` does not implement one. Blocks the mixed-vendor
  reviewer case above.
- **Actually deleting a role from the product.** GL03 §9 already scoped this out;
  unchanged. "Unseated for this run" is not "deleted".
- **A typed capability *request* from a role** — [SPEC-25](SPEC-25-expressibility-and-negotiation.md)
  Item 2's `needs: {capability: …}` channel. This spec answers the question at
  seat time; SPEC-25 answers it mid-turn. They share the `(role, capability)`
  dedupe key deliberately.
- **Consolidating the four role-tuple declarations** (`capabilities.py:104`,
  `:120`, `teamdraft.py:41`, and `topology.py:26-29`'s constants) behind one
  exported `CODING_ROLES` — a SPEC-19-shaped tidy-up this spec's drift lock makes
  visible but does not perform.
