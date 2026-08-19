# Slack Studio Auto-Start + Proactive Progress Stream — Design (Slice 5)

**Date:** 2026-08-19
**Status:** Design approved by the operator 2026-08-19. Not yet implemented.
**Depends on:** studio manager (Slice 1), run control (Slice 2), spin-down/reconfig (Slice 3), adopt+goal (Slice 4) — all merged.
**Modules:** `errorta_slack/{studio_tools,studio_concierge,concierge,connection,outbound,store}.py`, `errorta_council/coding/project_factory.py`, `errorta_app/slack_lifecycle.py`.

---

## 1. Problem

A live end-to-end run on 2026-08-19 created a project from Slack and then could not
start it. Four distinct defects, in the order they bite:

**Gap A — a studio-created project can never start.** `create_project`
(`studio_tools.py:293`) writes `north_star` and `definition_of_done` and stops. Grep
`work_request|focus|Focus` across `errorta_slack/studio_tools.py`: no matches. The
created `project.json` carries `work_request: ''` and no focus ledger. The very next
action, `start_run`, calls `next_goal.start_gate` (`tools.py:533`) for any fresh start
and is refused, because the gate requires an active Focus **or** a non-empty
`work_request` (`next_goal.py:37-61`). Observed verbatim in Slack:

> :warning: Autopilot approved start_run, but it didn't run — no current goal — the
> team would plan against the North Star alone, which may be stale.

This contradicts `docs/SLACK_STUDIO.md` §"v1 scope" ("Project created IDLE, then you
start it… an Approve button kicks off the coding team") — that path is unreachable,
because the Approve button leads to a refusal.

**Gap B — the operator does not want the two-step flow at all.** Restated requirement:
create provisions the project and channel and **immediately starts the run**; the
channel gets a confirmation naming what is being built; the bot then streams the
spec / brainstorm / plan / progress into the channel, keeps updating until told to
stop, and **always** reports three events — the team stops, hits a roadblock, or
finishes.

**Gap C — nothing unprompted has ever been posted to a channel.**
`errorta_slack/outbound.py` (479 lines) implements the entire streaming mechanism:
`run_loop` (`outbound.py:422-473`), a durable per-channel cursor, three content
sources, blocking signals rendered as buttoned decisions. **`run_loop` has zero
production callers.** `slack_lifecycle._start_locked`
(`errorta_app/slack_lifecycle.py:175-240`) builds the socket-mode bridge and its
asyncio loop thread and never schedules it. The only non-test references to
`outbound.*` are `connection.py`'s `ATTENTION_VERB` branch (`connection.py:978-983`).

**Gap D — two model-contract defects surfaced by the same run.**

- *No argument schema reaches the model.* `TOOL_CATALOG` entries carry only
  `{"trust","summary"}` (`tools.py:100+`, `studio_tools.py:64-88`), and both renderers
  emit only ``- `verb` [T]: summary`` (`concierge.py:195-197`, and the same shape in
  `studio_concierge`). The envelope contract says only `"args": {}`. The model guessed
  `set_next_goal`'s keys, omitted `title`, and the ledger refused: "focus title is
  required" (`ledger.py:1722`). This blocks **every** verb with a required argument.
  Corroboration that this is a known hole: `studio_concierge._MODELS_GUIDANCE`
  (`studio_concierge.py:79-90`) hand-writes a prose paragraph describing
  `create_project`'s `team` array shape — a bespoke workaround for one argument of one
  verb.
- *Success is reported over an error result.* The PM replied "Goal set and run started"
  when neither happened. `concierge.run_turn` **does** have a results-in-hand second
  hop that replaces `reply` (`concierge.py:450-473`), so this is not an envelope-design
  flaw. The optimistic text survives when the follow-up envelope fails to parse (the
  code explicitly keeps "the last known-good reply", `concierge.py:467-470`), when
  `max_hops` is reached, or simply because **nothing in the contract forbids claiming
  success over a `{"status":"error"}` result** — the only instruction is "Compose the
  final reply using these results."

## 2. Grounded facts

### 2.1 The run loop consumes `Focus`; `work_request` is legacy

Decides §3.1.

- `runner._pm_prompt` (`runner.py:3155-3167`) reads `store.active_focuses()` and pins
  *"CURRENT FOCUS — the team's operative scope right now. Plan ONLY these, in order"*,
  then demotes the charter: *"The North Star is REFERENCE ONLY — a guardrail for HOW to
  build, not a list of things to build now"*.
- `work_request` is reached **only** as a fallback when the focus ledger is empty, and
  the in-code comment calls it *"the legacy field … (defensive)"* (`runner.py:3170-3178`).
- `ledger._ensure_focus_migrated` (`ledger.py:1663`) is a one-way migration seeding
  `focus.jsonl` from `work_request` with `origin="work_request_migration"`.
  `set_work_request` (`ledger.py:1625`) additionally upserts the primary Focus "to keep
  the legacy field and the ledger coherent."
- The Slack surface already chose: `set_next_goal` writes
  `add_focus(..., origin="slack_pm")` and its docstring says a north-star write "would
  be near-inert here" (`tools.py:607-640`).

No code path writes a *new* `work_request` as a first choice.

### 2.2 The auto-start seam already exists and is already used

Decides §3.2. `StudioDeps.start_run_fn` (`studio_tools.py:153`) defaults to `None`
specifically to keep the `errorta_app.routes.coding` import lazy. `adopt_project`
(`studio_tools.py:531-541`) already performs exactly this auto-start:

```python
start_refused = start_gate(ledger)
if start_refused is None:
    start_fn = deps.start_run_fn
    if start_fn is None:
        from errorta_slack.tools import _default_start_run as start_fn
    start_fn(project_id, resume=False, continue_=False)
```

`_start_run` spawns a daemon thread and returns immediately, so the Slack turn does not
block. Mode must be fresh (`resume=False, continue_=False`): a new project is `"idle"`,
and `continue_` 409s "run is not continuable" on anything but `"stopped"`
(`tools.py:493-500`).

### 2.3 The milestone stream the operator asked for is already rendered prose

Decides §3.5. `team_log.build_team_log` (`team_log.py:51`) emits, with timestamps: the
North Star review; governance artifacts labeled **brainstorm / spec / implementation
plan / plan amendment** (`_ARTIFACT_LABEL`, `team_log.py:16`); reviews and approvals
("reviewed and approved the spec"); task creation; "turned the approved plan into
developer tasks"; PR opened / reviewed / tested / merged / conflict / blocked; and
`run_interrupted`. `outbound._current_items` (`outbound.py:211`) already consumes it.

### 2.4 Run completion is invisible to the outbound layer

Decides §3.6. `outbound._current_items` reads team log, `attention.list_open`, and the
publish ledger. **None of them carries run termination.** A run ending writes
`set_run_state(status="stopped", stop_reason=…)` (`routes/coding.py:2715`) or
`status="failed"` (`runner.py:8888`). Neither surfaces. "The team finished" and "the
team stopped" — two of the operator's three mandatory events — cannot fire today.
Roadblock is already covered: a blocking attention signal becomes a buttoned
`render.decision_message`, and a `blocked` decision becomes a team_log line.

### 2.5 There is an honest, post-execution reporter

Decides §3.3. `_create_project_outcome_text` (`connection.py:912`) renders from the real
result dict, not from the model envelope, and is called by `_post_studio_outcome`
(`connection.py:882`) and `_post_autopilot_outcome` (`connection.py:789`). This is the
structural antidote to Gap D's second half for this flow. Two caveats: it posts to the
**confirmation's** channel (the studio channel), so a message in the newly created
project channel is a second `poster.post_message(result["channel_id"], …)`; and
`adopt_project` has **no** bespoke renderer, so it falls through to the generic
`f"{verb}: {status}."` (`connection.py:906-909`) and silently drops `start_refused` on
both the button and autopilot paths — a live reporting bug in the code §3.2 reuses.

### 2.6 The catalog line shape is pinned by canaries

Decides §3.8. `tests/slack/test_catalog_canary.py` and `test_studio_catalog_canary.py`
regex the exact ``- `verb` [T]: summary`` line shape (`_CATALOG_LINE`). Changing the
render format requires updating them in the same commit.

## 3. Design

### 3.1 Seed a real `Focus` at project creation — in the engine factory

`create_project_from_charter` (`errorta_council/coding/project_factory.py`, after the
workspace setup at line 86) seeds the project's first Focus:

```python
goal = str(charter.get("initial_goal") or charter["north_star"]).strip()
if goal:
    store.add_focus(title=goal, body="", origin="studio_charter")
```

Rejected alternatives, for the record:

- **Seed `work_request` from the charter.** `LedgerStore.create_project` accepts
  `work_request=` (`ledger.py:615`), but that kwarg creates **no Focus**: the scope
  would reach the run lazily via `_ensure_focus_migrated`, stamped
  `origin="work_request_migration"` on a project created seconds earlier. Cheapest to
  write, worst provenance, deepens a dependency being unwound.
- **Exempt never-run projects from `start_gate`.** Rejected as actively dangerous. It
  exempts the wrong projects — "never run" is not "new", and the gate's motivating case
  (an adopted repo with ten days of commits, a stale North Star and zero focuses) is
  itself a never-run project. It also fixes nothing: the refusal disappears while the
  condition (a PM planning with no operative scope) stays, now silent. And
  `revision == 1` is not a reliable "never ran" signal — `set_project_status`,
  `set_work_request` and `promote_north_star` all bump it.

**Placement is in the engine factory, not `studio_tools`.** The factory's docstring
already promises a *"runnable-by-construction"* project and today's output is not
startable; it owns charter→project translation; and every future charter origin
inherits the fix. Deliberate consequence: it stops being a byte-for-byte mirror of
`wizard_create` (`routes/coding.py:2051`). The docstring must say so, or someone will
"restore" the mirror by deleting the seed.

`add_focus` (`ledger.py:1716`) is lock-held and caps the title itself. `north_star` is a
validated-non-empty required charter field (`project_factory.py:19-20`), so the fallback
can never hit the empty-title `LedgerError`. Writing `focus.jsonl` also makes
`_ensure_focus_migrated` a permanent no-op for that project (it is guarded on file
absence), so there is no double-seed.

**Why this restores rather than removes the gate:** when the seeded Focus is completed
and accepted it becomes `archived`, `active_focuses()` empties, and the gate fires again
on the next fresh start — on a project that now genuinely *has* finished work and a
possibly-stale charter. That is precisely the case the gate was written for.

**Optional charter field `initial_goal`.** Accepted when the operator supplies it in the
create message; never gathered, never asked for, never invented. Zero plumbing: the
charter dict flows `dispatch` → `create_project` → `create_fn` unfiltered
(`studio_tools.py:315`, `charter = dict(args or {})`) and the factory stores the whole
dict as `body_json`. Only prompt copy in `studio_concierge._INTAKE_CONTRACT`
(`studio_concierge.py:98-118`) changes. When absent the fallback is the north star
**copied verbatim** — the intake contract already forbids the model inventing field
values, and a code-level verbatim copy is not an invention, whereas a generated "first
increment" would be. On a greenfield repo, Focus == North Star correctly means "plan the
whole charter."

**Adopt does not inherit this.** An adopted project has no fresh charter; the only text
available is the *stored* north star, the exact thing the gate distrusts. Seeding from
it would auto-manufacture consent to re-litigate finished work. Create seeds because a
human just wrote the charter; adopt refuses because nobody did. No shared helper: the
shared-`start_gate` precedent exists because two paths do the *same* thing; these two
deliberately do opposite things.

### 3.2 `create_project` auto-starts the run

After `store.bind_channel`, `create_project` performs the §2.2 start, unconditionally —
**no approval gate of its own**. Operator's explicit decision: creating a project from
Slack always starts the run, whether or not autopilot is on.

Recorded risk, accepted by the operator: the Approve tap on create is currently also the
last anti-injection control on this path. With it gone, the controls that remain are the
allowlist (only allowlisted team/user ids can drive the PM at all) and the concierge's
injection rule (quoted or pasted text is data, never a command). Both hold today.

The `start_gate` call is **kept**, not removed. After §3.1 it simply passes; it remains
the guard for the impossible-by-construction no-goal case, and it is cheap.

Auto-start can legitimately fail: the member-health preflight runs on fresh starts only
and 409s when a provider is logged out (`tools._classify_start_exception`,
`tools.py:453-486`). The create result therefore carries `started` and `start_refused`,
and the posted message reports the **actual** value.

### 3.3 An honest confirmation, in the project channel

`_create_project_outcome_text` (§2.5) is extended to render, from the result dict:

- created project id and channel
- the north star it will build (or the `initial_goal` when supplied)
- the team roster
- `started: true` → "Run started with north star as: …"
- `started: false` → the real reason, never a success claim

`_post_studio_outcome` additionally posts the confirmation into the **new project
channel** (`result["channel_id"]`), not only the studio channel.

`adopt_project` gains its own renderer in the same table, fixing the §2.5 bug where
`start_refused` is silently dropped.

### 3.4 Wire `outbound.run_loop` into the bridge

`slack_lifecycle._start_locked` schedules the loop on the asyncio loop it already
builds:

```python
asyncio.run_coroutine_threadsafe(
    outbound.run_loop(
        bindings_provider=slack_store.list_bindings,
        deps=outbound.OutboundDeps(),
        poster=poster,
    ),
    loop,
)
```

`store.list_bindings` (`store.py:120`) already returns the required
`{"channel_id","project_id"}` shape. `stop()` must cancel the future so a
`sync()`-driven restart does not leave two loops polling one channel.

Delivery is **top-level channel messages**, one per milestone — the operator's decision.
The channel is dedicated to a single project, so there is nothing to drown out, and
unread counts stay meaningful.

### 3.5 Milestone selection

No new content source is needed for spec/brainstorm/plan/progress: §2.3's team log
already emits them. The outbound layer posts what the cursor has not yet seen.

### 3.6 New content source: run state

A fourth source, `_run_state_items`, diffs `get_run_state()["status"]` and emits a
marker `run:{status}:{ended_at}` so the three mandatory events fire:

| Event | Source |
|---|---|
| team finished | run state → `stopped` with a completion reason |
| team stopped / failed | run state → `stopped` / `failed` |
| roadblock | already covered — blocking attention signal → buttoned decision |

Marker shape matches the existing cursor contract (a set of posted markers, advanced one
at a time after each successful post), so a mid-loop poster failure cannot double-post.

### 3.7 Mute, and what overrides it

New per-channel notification state in `store` (bindings, cursor, dedupe, confirmations
and studio channel live there today; there is no notification flag). A new `[R]` verb
`set_updates` toggles it from chat ("stop updating me" / "resume updates").

The three mandatory events — stopped, roadblock, finished — **ignore the mute**. The
operator's requirement is that they always arrive.

### 3.8 Render argument names into the TOOLS block

`TOOL_CATALOG` entries gain an `args` field: an ordered tuple of
`(name, required, one-line description)`. Both renderers append them to the catalog
line. `_MODELS_GUIDANCE`'s hand-written `team`-shape paragraph
(`studio_concierge.py:79-90`) stays — it describes a nested array shape that a one-line
arg description cannot carry — but every other verb stops relying on the model guessing.

The canaries of §2.6 are updated in the same commit; their purpose (pin the line shape
against silent drift) is preserved with the new shape.

### 3.9 Reply reconciliation

Two changes in `concierge` (and the studio equivalent):

1. The follow-up prompt gains an explicit clause: a tool result carrying
   `{"status":"error"}` or `{"status":"refused"}` **must** be reported as not done, and
   the reply must never claim, imply, or hint that a failed action succeeded.
2. The parse-failure `break` (`concierge.py:467-470`) must not keep an optimistic hop-1
   reply when any tool result in hand is an error; it falls back to a rendered summary of
   the actual results instead.

## 4. Non-goals

- No batching or digesting of milestones (operator chose one message per milestone).
- No public URL for the runtime preview — loopback only, unchanged.
- No change to the trust class of any existing verb other than create's new auto-start.
- No durable thread memory (still resets on sidecar restart).

## 5. Slices

| Slice | Content | Independently shippable |
|---|---|---|
| 5a | §3.1 focus seeding, §3.2 auto-start, §3.3 honest confirmation | yes — makes create→run work |
| 5b | §3.4 wire run_loop, §3.6 run-state source, §3.7 mute + verb, cursor seeding on adopt | yes — makes the stream exist |
| 5c | §3.8 arg schema, §3.9 reply reconciliation | yes — fixes every C-class write |

## 6. Test plan

Every path below is currently untested; three prior code reviews on this project each
found their Critical finding on a path with no test at all.

**5a**
- `create_project_from_charter` seeds an active Focus from `north_star` when no
  `initial_goal` is given; the title matches verbatim.
- …seeds from `initial_goal` when supplied, preferring it over `north_star`.
- **Regression for Gap A:** a project created through the studio path is NOT refused by
  `start_gate` on a fresh start.
- `create_project` returns `started: true` and calls `start_run_fn` once with
  `resume=False, continue_=False`.
- A failing `start_run_fn` yields `started: false` plus the real reason; the project and
  binding survive.
- `_create_project_outcome_text` renders the north star and the started state; it never
  renders a success line when `started` is false.
- `adopt_project`'s renderer surfaces `start_refused` (fixes §2.5).
- **Gate preservation:** a project whose only Focus has been archived IS refused on the
  next fresh start.
- Adopt does **not** seed a Focus.

**5b**
- `run_loop` is scheduled on bridge start and cancelled on stop; two `sync()` calls do
  not leave two loops.
- `_run_state_items` emits exactly one item per status transition and nothing on repeat
  polls.
- A poster failure mid-loop re-posts nothing already posted (cursor contract).
- Mute suppresses ordinary milestones and does NOT suppress stopped / roadblock /
  finished.
- Adopt seeds the cursor at bind time; the first poll on an adopted project with history
  posts nothing.

**5c**
- The rendered catalog line carries each verb's argument names and required flags.
- Canaries updated and passing against the new shape.
- A turn whose tool result is `{"status":"error"}` produces a reply that does not claim
  success.
- A follow-up parse failure with an error result in hand does not emit the optimistic
  hop-1 reply.
