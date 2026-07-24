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
F159 hot-file serializer, `dev_repo_read` (`false`, Spec 11 — see below).

**Spec 11 — `dev_repo_read`.** When `true` (opt-in; default `false`) a DEV turn can READ its task
worktree in-turn: the `claude_cli` vendor runs with cwd set to the worktree and a
read-only tool allowlist (`Read`/`Grep`/`Glob` only — no write, no exec, no
network), and a raised turn budget, so the dev can grep the rest of the repo and
see both sides of a cross-file contract instead of reasoning from a pre-baked
half-context. The dev's actual edits still flow only through the `coding_turn.v1`
envelope (`execute_dev_turn`), never a Write tool. Planning/review turns and
non-`claude_cli` vendors are unaffected. Set `false` to restore the single-shot
empty-temp-dir behavior for dev turns.

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
    "checkpoint_cadence": "per_milestone",
    "checkpoint_n": 5,
    "completion_refused_limit": 2,
    "convergence_stall_limit": 20,
    "delivery_review_round_limit": 3,
    "dev_repo_read": false,
    "foundation_stall_limit": 12,
    "gate_bootstrap": true,
    "gate_min_merge_interval": 3,
    "gate_stall_limit": 8,
    "hot_file_escalation_threshold": 4,
    "hot_file_freeze_stall_limit": 15,
    "hot_file_threshold": 2,
    "max_iterations": 200,
    "max_model_calls": null,
    "max_parallel_workers": null,
    "member_failure_limit": 3,
    "model_escalation_limit": 2,
    "plan_streak_limit": 6,
    "pm_assist_limit": 1,
    "pm_idle_limit": 2,
    "review_min_latency_ms": 0,
    "review_screenshot": false,
    "reviewer_repo_read": false,
    "revise_chain_limit": 3,
    "revise_livelock_limit": 5,
    "task_reassignment_limit": 2,
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
