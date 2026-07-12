# F150 — `errorta team` builder: `create` / `add` (roles, counts, multi-model) + `--default`

**Target version:** v0.1 (CLI)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.

---

## Problem

Assembling a coding team from the CLI today is awkward:

- `errorta team set <role> <route>` sets **one member per role** (it dedups by
  `coding_role`), so a team with **three developers** can't be expressed — a
  second `team set dev …` overwrites the first.
- The only way to get N-of-a-role today is the raw `errorta run --members
  '<huge JSON array>'` escape hatch — unreadable, error-prone, and a terrible
  demo.
- There's no one-shot "give me a sensible team" — every member must be spelled
  out by exact route id, which a new user doesn't know.

Yet the **engine already supports multiple members per coding role**
(`members_by_coding_role` returns role→list; verified: a 6-member team of
1 pm / 3 dev / 1 reviewer / 1 tester resolves correctly). The gap is purely the
CLI surface.

We want a clean, legible team builder:

```sh
errorta team create --codingteam
errorta team add --pm       claude_cli.opus
errorta team add --dev      cursor_cli.composer-2.5 --count 3
errorta team add --tester   claude_cli.sonnet
errorta team add --reviewer claude_cli.sonnet
```

and a zero-config default:

```sh
errorta team create --codingteam --default
```

## Goals

- `team create` — start a fresh coding-team draft (optionally auto-filled).
- `team add --<role> <model|provider> [--count N]` — add N members of a role,
  with a single model **or** a whole provider (multi-model).
- `team create --default` — auto-assemble **1 PM · 3 devs · 1 reviewer ·
  1 tester**, choosing models from the user's connected providers by a
  documented, explainable policy.
- Legible output at every step (who's on the team, on what model) and thorough
  docs (`docs/CLI.md` + README).

## Non-goals

- Changing the engine's team/scheduler model (already supports N-per-role).
- Replacing `team set` / `team pool` / `team apply` — these stay (see
  [Relationship](#relationship-to-existing-team-subcommands)).
- Council-room ("answering") teams — `--codingteam` is the only team type today;
  the flag reserves space, `team room` still handles rooms.

---

## Command surface

> **Style note.** Your sketch used flag-form (`team --create --codingteam`,
> `team --addmember --pm …`). This spec maps those to **subcommands**
> (`team create`, `team add`) to stay consistent with the existing
> `team set` / `team apply` / `team clear`, while keeping the role as a flag
> (`--pm`/`--dev`/…) as you wrote it. The route/provider is a value (a flag like
> `--claude_cli.opus` can't be parsed — `.`/value), and the count is `--count N`.

### `team create [--codingteam] [--default]`

Starts a **fresh** coding-team draft for the bound project (clears any existing
draft first — equivalent to `team clear` then begin).

- `--codingteam` — explicit team type. Optional and currently the default/only
  type; reserved so a future `--councilteam` can diverge.
- `--default` — after creating, auto-assemble the default team (see
  [Default team](#default-team---default)). Mutually exclusive with a
  subsequent `team add` in the same breath is fine — `--default` just seeds the
  draft, which `team add` can then extend.

Output: the new (empty or seeded) draft, rendered like `team show`.

### `team add --<role> <model|provider> [--count N]`

Appends **N** members of `<role>` to the draft.

- **Role** (mutually-exclusive flags; a bare positional role is also accepted):
  `--pm` · `--dev` · `--reviewer` · `--tester`. Aliases: `--test`→tester,
  `--prog`/`--programmer`→dev. Roles map to the engine's canonical
  `CODING_ROLES = (pm, dev, reviewer, tester)`.
- **Model | provider** (positional value):
  - a **full route id** — `claude_cli.opus`, `cursor_cli.composer-2.5` →
    the member is **single-mode** on that route.
  - a **bare provider / CLI id** — `cursor_cli`, `claude_cli`, `anthropic` →
    the member is **multi-model**: `model_mode: "multi"` with `model_pool` =
    that provider's currently-available routes (from `GET /gateway/routes?
    provider=<p>`). This is the `#multi model` case in your sketch.
- **`--count N`** (default `1`; `-n N` alias) — create N members with ids
  `<role>-1` … `<role>-N`. If members of that role already exist, N more are
  appended (ids continue the sequence). `--pm` is capped at **1** (a second PM
  is rejected with a clear message).

Each added member is `{"id": "<role>-<n>", "role": "answerer", "enabled": true,
"metadata": {"coding_role": "<role>"}, …}` with either `gateway_route_id`
(single) or `model_mode:"multi"` + `model_pool` (provider) — exactly the shape
`teamdraft.set_route`/`set_pool` and run-setup already consume.

Output: the updated draft (`team show`), highlighting what was just added.

**Validation** (fail-closed, before writing the draft):
- Unknown role → error listing the four valid roles.
- A route id whose provider isn't connected, or a model not in that provider's
  route list → error naming the offending route and pointing at `errorta
  connect` / `errorta models`.
- A bare provider with **no** available routes (not connected / no models) →
  error.
- `--count < 1` or non-integer → error.

### Default team (`--default`)

`team create --codingteam --default` assembles:

| Role | Count | Model selection |
|---|---|---|
| pm | 1 | strongest available *reasoning* route |
| dev | 3 | best available *coding* route |
| reviewer | 1 | strong route, **preferring a different provider than dev** (review diversity) |
| tester | 1 | a capable mid route |

**Selection policy** (deterministic + explainable):

1. Fetch connected providers (`GET /gateway/providers`, `connected == true`) and
   their routes (`GET /gateway/routes`). If **zero** connected providers → error:
   "no connected providers — run `errorta connect <provider>` first."
2. Tag each available route with a coarse capability class by model-name
   heuristic (documented, overridable later by model-learning stats):
   - **reasoning-strong**: `opus`, `gpt-5`, `o1`/`o3`, `gemini-*-pro`
   - **coding-strong**: `composer`, `*-codex`, `sonnet`, `deepseek-coder`
   - **mid**: `sonnet`, `gpt-5-mini`, `composer` (a route may be both)
   - **light**: `haiku`, `*-mini`, small local (`qwen:7b`) — avoided for pm/dev
3. Assign by role using an ordered preference over the tags, first available wins:
   - **pm** → reasoning-strong → coding-strong → mid → (single best available)
   - **dev** → coding-strong → mid → (best available); all 3 devs share the pick
     unless the user later edits.
   - **reviewer** → a strong route from a provider **≠** the dev's provider if
     one exists (diversity), else best strong route.
   - **tester** → mid → coding-strong → (best available).
4. If the same route is the only viable one, it's reused across roles (with a
   `log()` note that diversity couldn't be honored).
5. **Print the assignment + the one-line rationale per role** so the choice is
   transparent (important for the marketing demo).

The heuristic is intentionally simple and self-documenting; a follow-up can weight
it by the F129 model-learning acceptance stats (`GET /model-learning`).

---

## Relationship to existing `team` subcommands

`create` / `add` are **additive**. Unchanged:

- `team set <role> <route>` — still the one-liner for a single member of a role
  (equivalent to `team add --<role> <route> --count 1` when the role is empty).
- `team pool <role> <r,r>` — explicit multi-mode over specific routes (the `add`
  provider form is sugar over this).
- `team mode` / `enable` / `disable` / `room` / `preflight` / `apply` / `clear`
  / `show` — unchanged.

The full lifecycle after building:

```sh
errorta team apply --yes     # writes the draft into run-setup
errorta run --yes            # or: errorta run --yes  (run consumes the draft)
```

`team apply` / `run` need no change — they already accept a multi-member draft.

---

## UX flow (the marketing demo, comment-free)

```sh
errorta connect cursor cli
errorta connect claudecode cli
errorta new reddit-clone ~/dev --north-star "A Reddit-style link-sharing app: subreddits, posts, threaded comments, and up/down voting" --yes
errorta team create --codingteam
errorta team add --pm claude_cli.opus
errorta team add --dev cursor_cli.composer-2.5 --count 3
errorta team add --reviewer claude_cli.sonnet
errorta team add --tester claude_cli.sonnet
errorta team apply --yes
errorta run --yes
```

Zero-config variant:

```sh
errorta new reddit-clone ~/dev --north-star "…" --yes
errorta team create --codingteam --default
errorta team apply --yes
errorta run --yes
```

Multi-model variant (provider-only routes):

```sh
errorta team add --pm claude_cli.opus
errorta team add --dev cursor_cli --count 3
errorta team add --reviewer claude_cli
errorta team add --tester claude_cli
```

---

## Implementation notes

- **`python/errorta_cli/teamdraft.py`**
  - The dedup-by-role `_find` is the blocker for N-of-a-role. Add
    `add_members(draft, role, route_or_provider, count, *, multi)` that appends
    members with sequential ids `<role>-<n>` and `coding_role=role`, using the
    existing single/multi shapes. Keep `set_route`/`set_pool` for the
    one-per-role path.
  - A helper to resolve a bare provider → `model_pool` (needs the routes list;
    the command passes it in from `GET /gateway/routes`).
- **`python/errorta_cli/commands/team.py`**
  - New subcommands `create` and `add` in `_call`. `create` clears the draft
    (and, with `--default`, calls the assembler). `add` parses role flags +
    value + `--count`, fetches routes to validate/expand, and appends.
  - Role flags (`--pm/--dev/--reviewer/--tester/--test`) + `--count`/`-n` +
    `--default`/`--codingteam` as `Param`s on the `team` command.
  - Default assembler: `GET /gateway/providers` + `GET /gateway/routes`, apply
    the policy, build the draft, render the assignment + rationale.
- **`python/cli.spec`** — `team` is already a bundled command module; no new
  module. If the assembler moves to its own file, add it to `_CLI_COMMAND_MODULES`.
- **No server change required** — `_resolve_members` / `_ensure_coding_roles`
  already accept the multi-member body (verified). The command only reads
  `/gateway/providers` + `/gateway/routes` (existing endpoints).

## Edge cases

- `team add` before `team create` (no draft) → auto-create an empty draft, then
  add (don't error).
- `--count` on `--pm` > 1 → reject ("a coding team has one PM").
- Re-running `team add --dev … --count 3` → appends (ids continue: dev-4..6).
  `team create` first to start clean.
- Bare provider with exactly one available model → multi-pool of one = behaves
  like single; fine.
- A route the user's provider lists but that later disappears (e.g. CLI logged
  out) → surfaced at `team preflight` / `apply`, not at `add` time.
- `--default` with only one connected provider → all roles use it; log that
  reviewer-diversity was not possible.
- `--default` with only a *light* model available → assign it but warn it may be
  underpowered for pm/dev.

## Documentation (required — part of acceptance)

- **`docs/CLI.md`** — add `create` / `add` to the Providers & team section's
  command table; a "Build a coding team" guide subsection covering roles,
  `--count`, single-vs-provider (multi-model), `--default` and its policy, and
  the full lifecycle (`create → add → apply → run`). Update any `team set`-only
  examples to show the richer builder.
- **`README.md`** — update the Headless-CLI quickstart to use
  `team create --default` (or the explicit builder) instead of a lone
  `team set dev …`, so the marquee example shows a real multi-role team.
- Keep the `team --help` usage string in sync.

## Testing

- **teamdraft unit**: `add_members` produces N members, sequential ids,
  correct `coding_role`, single vs multi shapes; pm cap; append semantics.
- **command unit** (mocked `/gateway/*`): role-flag parsing + aliases; value =
  route → single, value = provider → multi pool from the mocked routes;
  `--count`; unknown role / disconnected provider / empty provider errors;
  `create` clears; `create --default` assembles 1/3/1/1 with the policy;
  assignment rationale rendered; `add` before `create` auto-creates.
- **default-policy unit**: deterministic assignment across representative
  provider sets (only-cursor, only-claude, both, only-light) — exact expected
  routes per role, including the reviewer-diversity preference and the
  single-provider fallback.
- **resolution parity**: the draft `team apply` builds resolves (via
  `_resolve_members`) to the intended role counts (regression-lock the
  1 pm / 3 dev / 1 reviewer / 1 tester shape).
- **doc lint**: `docs/CLI.md` examples reference only real commands/flags.

## Out of scope / follow-ups

- Weighting `--default` by F129 model-learning acceptance stats.
- `team add --model-mode multi <r,r>` explicit multi over chosen routes (already
  `team pool`; could unify).
- Per-member overrides after `--default` (already possible via `team set`/`pool`).
- Council-room ("answering") team building.
