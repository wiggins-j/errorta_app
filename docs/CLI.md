# Errorta headless CLI

`errorta` is the terminal front-end for the Errorta Coding Council. It drives the
same local engine the desktop app uses — spin up a coding team, watch it work,
steer it, and ship a PR — without leaving your shell. It is a thin **client** of
a local sidecar server; the CLI binary carries that server inside itself, so
there is nothing else to install or run.

- Everything is **local**. The CLI talks to a loopback sidecar; your code and
  keys never leave the machine (unless you configure a remote provider yourself).
- It shares the **same on-disk store** as the desktop app, so a project is
  interchangeable between the terminal and the GUI (see
  [One sidecar, one owner](#one-sidecar-one-owner)).

---

## Contents

- [Quickstart](#quickstart)
- [Concepts](#concepts)
- [Install](#install)
- [First run](#first-run)
- [Command reference](#command-reference)
  - [Global options](#global-options)
  - [Sidecar lifecycle](#sidecar-lifecycle)
  - [Project lifecycle](#project-lifecycle)
  - [Providers & team](#providers--team)
  - [Run control](#run-control)
  - [Observability & logs](#observability--logs)
  - [Mid-run steering](#mid-run-steering)
  - [Files & delivery](#files--delivery)
  - [Runtime & tests](#runtime--tests)
  - [Grounding](#grounding)
- [Guides](#guides)
  - [The team → run lifecycle](#the-team--run-lifecycle)
  - [Watching a run: the verbosity model](#watching-a-run-the-verbosity-model)
  - [Reading the logs: which command for what](#reading-the-logs-which-command-for-what)
  - [Steering a run mid-flight](#steering-a-run-mid-flight)
  - [Publishing & the outward-action confirmation](#publishing--the-outward-action-confirmation)
  - [Running the delivered program](#running-the-delivered-program)
  - [Scripting & CI: `--json` and exit codes](#scripting--ci-json-and-exit-codes)
  - [One sidecar, one owner](#one-sidecar-one-owner)
- [Troubleshooting & FAQ](#troubleshooting--faq)

---

## Quickstart

From an empty terminal to a running coding team in six commands. Each line is
copy-pasteable.

```bash
chmod +x ./errorta && mv ./errorta /usr/local/bin/errorta
```

Connect a model provider. The key is read from a hidden prompt, never from the
command line:

```bash
errorta connect anthropic api
```

Create a project rooted in the current directory:

```bash
cd ~/code/my-project
errorta new my-project --here --north-star "A CLI that renders Markdown tables"
```

Pick who does the work, then commit that team to the run config:

```bash
errorta team set dev anthropic.claude-sonnet-4-6
errorta team apply --yes
```

Start the run and stream it live until it finishes:

```bash
errorta run --yes
```

Watch the team log from another terminal, then merge the delivered code into your
real files when you like what you see:

```bash
errorta log --watch
errorta accept --yes
```

That is the whole loop. Everything below is the full reference and the guides for
each of those steps.

---

## Concepts

A handful of ideas explain how every command behaves.

- **The CLI is a client of a local sidecar.** The `errorta` binary embeds a
  FastAPI server and talks to it over loopback HTTP. You never start the server
  yourself — the CLI adopts a running one or spawns its own.
- **One command tree, two surfaces.** Every capability exists once and is
  reachable two ways: as an interactive REPL slash-command (`/status`,
  `/log --role dev`) and as a scriptable subcommand (`errorta status`,
  `errorta log --role dev --json`). They call the exact same route, so the two
  surfaces can't drift.
- **Reads vs. writes.** Most commands are pure reads (no side effects; safe to
  re-run). Commands that write to the shared store take a `--yes` flag; in a
  terminal they prompt for confirmation, and **non-interactively (`--json`, CI,
  pipes) they require `--yes`** or they refuse. These are flagged ‡ in the
  reference below.
- **Layered verbosity.** A single 0–5 level dials how much the live view shows,
  and per-channel `/watch`, `/mute`, and `/solo` let you drill into one stream
  without cranking the whole level.
- **Sole owner, single instance.** Exactly one sidecar owns a given data store.
  The CLI won't run a second sidecar next to the desktop app against the same
  store — close the app, or point the CLI at a separate `--home`.
- **Keys never via argv.** An API key is always read from a hidden prompt or a
  file you name, never passed as a command argument (that would leak it into your
  shell history and process list).

---

## Install

### Option A — the single binary (recommended)

`errorta` ships as one self-contained executable. It is both the CLI and, via a
hidden `errorta __serve__` subcommand, the embedded sidecar server — no Python,
no separate server process.

Download the `errorta` binary for your platform, put it on your `PATH`, and make
it executable:

```bash
chmod +x ./errorta
mv ./errorta /usr/local/bin/errorta   # or anywhere on your PATH
errorta --help
```

> **Building the binary (maintainers).** The binary is produced with PyInstaller
> from a fully-configured environment (the engine + AIAR installed):
> `pyinstaller python/cli.spec` → `dist/errorta`. This is a maintainer/CI step,
> not part of the day-to-day dev loop.

### Option B — from source (developers)

If you have the repo checked out with the Python environment set up (see
`DEVELOPING.md`), the `errorta` command is installed with the package:

```bash
cd python
pip install -e .
errorta --help
```

In this mode the CLI re-execs `python -m errorta_cli __serve__` to run its
sidecar; in the frozen binary it re-execs itself. Either way you get one
process tree that owns exactly one sidecar per data store.

### Option C — run from a checkout without installing (`dev-errorta`)

For hacking on the CLI, `scripts/dev-errorta` runs it straight from the source
tree. One-time: create the venv (installs the engine + CLI + AIAR editable), then
put `errorta` on your `PATH`:

```bash
scripts/setup-cli-venv.sh
ln -sf "$PWD/scripts/dev-errorta" /usr/local/bin/errorta
errorta --help
```

`dev-errorta` finds its Python in this order: `$ERRORTA_PY`, then
`python/.venv/bin/python`, then `python3` on `PATH`. So if you already have a
Python env with the engine deps (fastapi + uvicorn + aiar), you can skip the
venv step and just point at it:

```bash
ln -sf "$PWD/scripts/dev-errorta" /usr/local/bin/errorta
export ERRORTA_PY=/path/to/that/python   # e.g. add to ~/.zshrc
errorta connect status
```

---

## First run

Point your terminal at a code repository and launch the interactive session:

```bash
cd ~/code/my-project
errorta
```

On a fresh machine — before any AI provider is connected — you'll see a short
welcome that points you at the next step. Connect a provider, and it stops
appearing. To silence it entirely, pass `--no-onboarding` or set
`ERRORTA_NO_ONBOARDING=1`.

### Connect a provider

An API key is **never** passed as a command argument (that would leak it into
your shell history). It is read from a hidden prompt, or from a file you name:

```bash
errorta connect anthropic api                 # prompts for the key (input hidden)
errorta connect openai api --key-file ./k.txt # reads the first line of a file
errorta connect claudecode cli                # a subscription CLI (no key stored)
errorta connect ollama                        # local models via Ollama
errorta connect status                        # what's configured / connected
```

Supported targets: `anthropic` / `openai` / `google` (API keys),
`claudecode` / `codex` / `cursor` (subscription CLIs), `ollama` (local), and
`custom <alias>` for any OpenAI- or Anthropic-compatible endpoint (e.g. a
self-hosted server at `http://example-host:1234`).

### Scope a project

Let the PM interview you and draft the team + first tasks:

```bash
errorta wizard
```

…or create a project directly and bind it to the current directory:

```bash
errorta new my-project --here
```

…or adopt an existing repository you're standing in:

```bash
errorta import local .
```

Either way a `.errorta-project` pointer is written in the directory so future
`errorta` invocations from here (or the GUI) resolve to the same project.

---

## Command reference

Every command lives in one command tree and is reachable both interactively
(`/log`) and as a subcommand (`errorta log`). Many commands take a **sub-verb**
and one or two positional arguments — `errorta team set dev <route>` means
sub-verb `set`, then `dev`, then the route. You can always append `--help` to any
command for its own flags.

Legend:

- **‡** — writes to the shared store. Prompts for confirmation in a terminal;
  **requires `--yes` when run non-interactively** (`--json`, CI, or a pipe). A
  few of these (e.g. `north-star`, `team`, `pm`, `governance`, `attention`,
  `grounding`, `runtime`) have both read and write sub-verbs — only the writing
  sub-verbs need `--yes`.

### Global options

These apply to every command and may appear before or after the subcommand
(`errorta status --json` and `errorta --json status` are equivalent).

| Option | Purpose |
|---|---|
| `--home <path>` | Override `ERRORTA_HOME` — drive an isolated store. |
| `--verbosity <0..5>`, `-V` | Global verbosity level or name (`quiet`…`firehose`). Also `ERRORTA_CLI_VERBOSITY`. |
| `--no-spawn` | Never spawn a sidecar; error if none is already running (CI-safe). |
| `--json` | Emit the raw route payload as JSON to stdout; no prompts, no live view, no onboarding. |
| `--poll-interval <sec>` | Seconds between `--watch` re-renders / poll ticks. |
| `--no-onboarding` | Suppress the first-run welcome hint (also `ERRORTA_NO_ONBOARDING=1`). |

### Sidecar lifecycle

Not part of the command tree — a small management group for the CLI-owned
sidecar. See [One sidecar, one owner](#one-sidecar-one-owner).

| Command | What it does |
|---|---|
| `errorta sidecar status` | Whether a CLI-owned sidecar is running, and on what port / pid. |
| `errorta sidecar stop` | Stop the CLI-owned sidecar and drop its record. |
| `errorta sidecar restart` | Stop the current CLI sidecar and spawn a fresh one. |

### Project lifecycle

| Command | What it does | Key flags |
|---|---|---|
| `new <id>` ‡ | Create a greenfield project and bind this directory to it. | `--here`, `--delivery-root`, `--north-star`, `--dod`, `--work-request`, `--id`, `--yes` |
| `import <local\|github> <path\|url>` ‡ | Import an existing project (local folder or GitHub clone). | `--sub`, `--id`, `--branch`, `--git-init`, `--yes` |
| `projects` | List all coding projects (with derived status). | |
| `open <id>` | Bind this directory to a project and show it. | `--id` |
| `switch <id>` | Switch the session to another project (alias of `open`). | `--id` |
| `delete <id>` ‡ | Delete a project (refused while a run is active). | `--id`, `--yes` |
| `north-star [show\|set\|proposal\|accept]` ‡ | Show / set the North Star + Definition of Done, or accept a PM-inferred proposal. | `--north-star`, `--dod`, `--yes` |
| `focus [list\|add\|edit\|reorder\|accept\|work-request]` ‡ | List / add / edit / reorder / accept Current Focus goals. | `--status`, `--title`, `--body`, `--yes` |

### Providers & team

| Command | What it does | Key flags |
|---|---|---|
| `connect [<target> <kind>\|status]` ‡ | Configure AI providers (keys / CLIs / ollama / custom) and show status. | `--key-file`, `--binary`, `--login`, `--host`, `--base-url`, `--api-style`, `--model`, `--auth-header`, `--auth-prefix`, `--yes` |
| `team [show\|create\|add\|set\|pool\|mode\|enable\|disable\|room\|preflight\|apply\|clear]` ‡ | Show / build / apply the coding team (draft → run-setup). | `--default`, `--count`, `--pm\|--dev\|--reviewer\|--tester`, `--yes` |
| `wizard` ‡ | Conversational project + team setup (AI Wizard). | `--model`, `--project`, `--delivery-root`, `--yes` |
| `models` | What the PM learned (cross-project) + this project's assignments. | |

The `team` sub-verbs edit a **local draft** (no store write). Only `team apply` (‡)
writes the draft to the run config; `team preflight` probes member health.

**Build a coding team (F150).** The canonical coding roles are `pm`, `dev`,
`reviewer`, `tester`, and the engine supports **multiple members per role**:

```sh
errorta team create --codingteam
errorta team add --pm       claude_cli.opus
errorta team add --dev      cursor_cli.composer-2.5 --count 3
errorta team add --reviewer claude_cli.sonnet
errorta team add --tester   claude_cli.sonnet
errorta team apply --yes
```

- `team create [--default]` starts a fresh draft. `--default` auto-assembles
  **1 pm / 3 dev / 1 reviewer / 1 tester**, picking models from your usable
  providers (reasoning-strong PM, coding-strong devs, a reviewer from a different
  provider for diversity) and printing the assignment + rationale.
- `team add --<role> <value> [--count N]` appends N members of a role. `<value>`
  is a **full route id** (`claude_cli.opus`) for a single model, or a **bare
  provider** (`cursor_cli`, `claude_cli`) for a multi-model member pooled over
  that provider's routes. Discover routes with `errorta models`.
- `--count` on `--pm` is capped at 1. "N devs" is *capacity*: parallel dev work
  ramps as the project's foundation lands and the PM splits the backlog.

The lower-level `set <role> <route>` (one per role), `pool <role> <r,r,…>`,
`mode`, `enable\|disable`, and `room <room_id>` (Council-room backing) still work.
Note: these key on `coding_role`, so on a multi-member role (e.g. 3 devs) they act
on the **first** member — use `team create` + `team add` to (re)build the set.

### Run control

| Command | What it does | Key flags |
|---|---|---|
| `setup` ‡ | Show / preflight / confirm run setup — the readiness gate. | `--preflight`, `--confirm`, `--room`, `--governance`, `--guardrail`, `--max-iterations`, `--max-model-calls`, `--max-parallel`, `--max-review-rounds`, `--checkpoint-cadence`, `--checkpoint-n`, `--block-on-problems`, `--human-code-approval`, `--member-failure-limit`, `--preflight-enabled`, `--yes` |
| `run` ‡ | Start a fresh run and stream the live view until it finishes. | `--room`, `--members`, `--detach`, `--yes` |
| `cancel` ‡ | Request cancellation of the running run (observed at the next turn boundary). | `--yes` |
| `resume` ‡ | Resume an interrupted run (recovers its saved team). | `--room`, `--members`, `--yes` |
| `continue` ‡ | Continue a run that stopped at a governance gate (F100). | `--room`, `--members`, `--yes` |

`run` streams the live view to completion on its own — `--watch` is redundant on
it (the CLI notes this and continues). Use `--detach` to fire the run and return
immediately. There is **no pause**: a pause is a checkpoint plus `resume`.

### Observability & logs

All read-only (except `attention`'s resolve sub-verb). Add `--watch` to
re-render on the poll loop; re-run any of them anytime for a fresh snapshot.

| Command | What it does | Key flags |
|---|---|---|
| `status` | Sidecar health + the bound project's run state (`state`, last `stop_reason`, counters). | |
| `log` | Team Log narrative, colorized by role. | `--role`, `--member`, `--grep`, `--watch` |
| `decisions` | Decision event stream (`--kind` supports globs, e.g. `pr_*`). | `--kind`, `--watch` |
| `turns` | Per-turn transcript (role / route / outcome / tokens). | `--limit`, `--watch` |
| `turn` | One turn's transcript + Context Report. | `--task`, `--turn` |
| `board` | Backlog as todo / doing / blocked / done columns. | `--watch` |
| `tasks` | Backlog as a compact status table. | `--watch` |
| `prs` | Pull requests (branch-per-task review / test / merge state). | `--watch` |
| `pr` | One PR's detail + worktree diff (via `delta`/pager if present). | `--id` |
| `tokens` | Token usage rollup (by role / route / member; measured vs. estimated). | `--watch` |
| `attention [(read)\|resolve <signal>]` ‡ | Problems + alerts (read), or resolve a signal. | `--state`, `--action`, `--suggestion-id`, `--correction-file`, `--watch`, `--yes` |

### Mid-run steering

Everything here works while a run is live.

| Command | What it does | Key flags |
|---|---|---|
| `interject "<directive>"` ‡ | Send an authoritative directive to the PM (consumed on the next plan turn). | `--artifact-id`, `--yes` |
| `pm [chat\|changes\|ask\|control\|accept\|decline\|"<question>"]` ‡ | Read PM chat / changes, or steer (ask, control-actions, accept / decline a PM Change). | `--actions`, `--watch`, `--yes` |
| `governance [show\|settings\|approve\|reject\|artifact]` ‡ | Read governance state, or steer (settings, approve / reject, artifacts). | `--mode`, `--phase`, `--human-code-approval`, `--max-review-rounds`, `--block-on-problems`, `--monitor`, `--feedback`, `--target-path`, `--title`, `--watch`, `--yes` |
| `task [new\|set]` ‡ | Add (`new`) or edit (`set`) a backlog task — works mid-run. | `--role`, `--detail`, `--state`, `--title`, `--depends-on`, `--yes` |

`pm control` takes a JSON array of control-actions via `--actions`
(`assign_models`, `set_autonomy`, `set_governance`, …); each produces a reviewable
**PM Change** you can `pm accept <id>` or `pm decline <id>`.

### Files & delivery

| Command | What it does | Key flags |
|---|---|---|
| `files` | Show a delivered file on master (content + sha). | `--a` (path) |
| `edit <path>` ‡ | Edit a delivered file (`--content-file` or `$EDITOR`; never via argv). | `--content-file`, `--yes` |
| `diff` | Worktree diff preview of the delivered code. | `--watch` |
| `accept` ‡ | Merge-back the delivered tree into your real files (deliberate accept). | `--override`, `--allow-conflicts`, `--yes` |
| `publish [targets\|events\|auth-status\|manual-export\|pr\|new-repo]` ‡ | Export the delivered work, open a PR, or create a repo (outward-facing). | `--kind`, `--name`, `--public`, `--local-only`, `--branch`, `--title`, `--body`, `--override`, `--yes` |

### Runtime & tests

| Command | What it does | Key flags |
|---|---|---|
| `runtime [(profiles)\|detect\|setup\|start\|stop\|run\|run-cli\|logs\|health\|test\|repair]` ‡ | Run the delivered program: read profiles, or detect / set up / launch / probe it. | `--p1`, `--p2`, `--session`, `--kind`, `--args`, `--timeout`, `--go`, `--reduced-isolation`, `--profile`, `--watch`, `--yes` |
| `test-commands [show\|set]` ‡ | Show or set the project's merge-gate test commands. | `--commands`, `--yes` |
| `test-settings [show\|set]` ‡ | Show or set project test settings (`require_sandbox`). | `--require-sandbox`, `--yes` |
| `test-runs` | List the recorded test-command runs. | |

`runtime run` produces a **preview** by default; add `--go` to actually launch the
program. `runtime logs <session>` tails a session's logs (`--watch` follows).

### Grounding

| Command | What it does | Key flags |
|---|---|---|
| `grounding [binding\|corpora\|capabilities\|retrieve\|bootstrap\|memory\|build-from-project\|working-memory]` ‡ | Project corpus binding + retrieval + memory (grounding). | `--p1`, `--mode`, `--corpus`, `--source-root`, `--q`, `--k`, `--watch`, `--yes` |

Read views (`corpora`, `capabilities`, `retrieve --q "<query>" --k 6`,
`working-memory`) need no `--yes`; binding and memory writes (`binding set`,
`memory rebuild`, `bootstrap`, `build-from-project`) do.

---

## Guides

### The team → run lifecycle

A fresh project can't run until you've (a) chosen a team and (b) confirmed run
setup. The order is always **team → apply → run**:

```bash
errorta team set dev anthropic.claude-sonnet-4-6
errorta team set reviewer openai.gpt-5
errorta team show
```

`team set/pool/mode/enable/disable` edit a **local draft** — no store write, no
run yet. When the draft looks right, commit it:

```bash
errorta team apply --yes
```

`apply` writes the resolved members into the project's run config (the same shape
the desktop app writes). You can probe provider health before committing to a
run:

```bash
errorta team preflight
```

Then confirm the readiness gate and start. `setup` shows and edits governance,
guardrails, and caps; `setup --confirm` marks the project run-ready:

```bash
errorta setup
errorta setup --confirm --governance autonomous --max-iterations 40 --yes
errorta run --yes
```

If you skip straight to `run` on an unconfirmed project, the CLI reports
`run_setup_required` (exit code 12) and tells you to run `setup --confirm` first.

### Watching a run: the verbosity model

The live view is **layered**. A single global level 0–5 unlocks a fixed set of
channels; you don't have to drown in output to see the one stream you care about.

| Level | Name | Adds channels |
|------:|------|---------------|
| 0 | `quiet` | (headlines only: run started/stopped + `stop_reason`, PR opened/merged, blockers) |
| 1 | `default` | team-log, attention, prs |
| 2 | `verbose` | + decisions, runtime (task transitions / test runs / launches) |
| 3 | `debug` | + turns, tokens |
| 4 | `trace` | + tools (tool events, prompt/response) |
| 5 | `firehose` | + poll, http (raw poll diffs / HTTP trace) |

Set it globally — per invocation, via env, or live in the REPL:

```bash
errorta run --yes -V verbose      # start at level 2
export ERRORTA_CLI_VERBOSITY=1    # default for this shell
```

Inside the interactive session, dial it live and drill into single channels
independent of the level:

```text
/verbosity 3      set the global level live
/watch tools      force-show one channel without cranking the whole level
/mute prs         force-hide a channel
/solo team-log    show only this channel
/unsolo           clear the solo
```

So you can sit at `default` and `/watch tokens` when you want cost detail, then
`/mute` it again — precise focus instead of an all-or-nothing firehose. The known
channels are `team-log`, `attention`, `prs`, `decisions`, `runtime`, `turns`,
`tokens`, `tools`, `poll`, and `http`.

### Reading the logs: which command for what

Errorta records several append-only ledgers; each read command is a lens on a
different one. Reach for the right one instead of scrolling everything:

- **`log`** — the human Team Log narrative, colorized by role. Filter it with
  `--role dev`, `--member m-2`, or `--grep pygame`. This is the "what is the team
  doing right now" view. `errorta log --watch` tails it.
- **`decisions`** — the structured decision stream (task claimed, PR opened,
  review verdict, merge). `--kind pr_*` globs to one family of choices. Use this
  when you want the skeleton of *what happened* without the prose.
- **`turns`** / **`turn`** — per-turn transcript. `turns` is the list
  (role / route / outcome / tokens); `turn --task <t> --turn <id>` opens one
  turn's full transcript plus its Context Report (exactly what went into that
  prompt). This is the deepest observability pull.
- **`board`** / **`tasks`** — the backlog. `board` shows todo/doing/blocked/done
  columns; `tasks` is a compact one-line-per-task table.
- **`tokens`** — the usage rollup by role / route / member, with an honest
  measured-vs-estimated meter so you can trust the numbers.
- **`prs`** / **`pr`** — pull-request state and a single PR's diff.
- **`runtime logs <session>`** — the delivered program's own stdout/stderr from a
  launch or test session (distinct from the team log).
- **`status`** — the one-glance summary: sidecar health, run `state`, last
  `stop_reason`, and any blockers.

### Steering a run mid-flight

You don't have to stop a run to redirect it. All of these apply live:

```bash
errorta interject "prioritize the parser over the CLI flags" --yes
```

An interjection is **authoritative** — the PM consumes it on its next plan turn
and re-plans without a restart. To have a conversation with the PM instead:

```bash
errorta pm "why did you drop the caching task?"
errorta pm changes                 # pending PM Changes
errorta pm accept <change_id>      # keep a change (or: pm decline <change_id>)
```

Structured control actions (reassign models, change autonomy/governance) go
through `pm control` with a JSON array and land as reviewable PM Changes:

```bash
errorta pm control --actions '[{"action":"assign_models","role":"dev","route":"anthropic.claude-sonnet-4-6"}]' --yes
```

You can also add or re-scope backlog tasks and Current Focus goals mid-run:

```bash
errorta task new "add a --no-color flag" --role dev --yes
errorta focus add --body "ship the MVP before adding config" --yes
```

Note that a couple of accepts (a North-Star proposal, a focus accept) are blocked
while a run thread is live — the CLI explains and lets you queue or cancel rather
than failing with a raw error.

### Publishing & the outward-action confirmation

Delivery has two distinct steps, and it helps to keep them straight:

- **`accept`** merges the delivered tree back into *your own local files*. It's a
  local, deliberate action gated behind the merge gate (`--override` to merge
  despite a blocked gate; `--allow-conflicts` to permit conflicting files).
- **`publish`** is **outward-facing** — it exports the work or pushes it somewhere
  others can see it. Because opening a public PR or creating a repo leaves your
  machine, publish confirms before firing, and **non-interactively it requires an
  explicit `--yes`** (no silent public-repo creation from a script).

```bash
errorta publish targets                        # what publish can do here
errorta publish manual-export --kind zip        # local export, no network
errorta publish pr --branch errorta/my-fix --yes
errorta publish new-repo my-project --public --yes
```

`new-repo` is **private by default**; `--public` is the deliberate opt-in.
`--local-only` creates a local git repo with no GitHub push. A secret scan blocks
`pr` / `new-repo` if it finds credentials; `--override` bypasses it (use with
care). `publish auth-status` shows whether your GitHub auth is ready.

### Running the delivered program

For a runnable deliverable (a game, a server, a CLI), Errorta can launch the
delivered code to prove it actually starts. The engine already does this in-loop
as part of the F146 delivery review — a final reviewer pass, a final test run, and
a headless launch probe — so "done" means the code was reviewed, tested, *and*
ran. From the CLI you can also drive it directly:

```bash
errorta runtime                         # read the detected run profiles
errorta runtime detect --yes            # (re)detect how to run this project
errorta runtime setup --yes             # install deps (e.g. a managed venv)
errorta runtime run                     # PREVIEW the launch (no process yet)
errorta runtime run --go --yes          # actually launch it (bounded, headless)
errorta runtime logs <session> --watch  # tail that session's output
```

Launches are offscreen by default (`SDL_VIDEODRIVER=dummy` and friends), so this
works on a headless box. `runtime test` records a runtime-test result; screenshots
captured server-side are surfaced as a path (inlined on iTerm/kitty).

### Scripting & CI: `--json` and exit codes

Every command takes a global `--json` flag that prints the raw route payload to
stdout — stable, parseable, and free of any decorative rendering:

```bash
errorta status --json | jq '.run.state'
errorta --json --no-spawn tasks          # never spawn a sidecar; error if none is up
errorta run --json --yes                 # fire a run, machine-readable, gate a pipeline
```

`--json` is strictly non-interactive: it never prompts, never streams a live view,
and never prints the onboarding hint. A command that needs a provider — or a
running sidecar, or a `--yes` for a mutation — exits non-zero with a
machine-readable error instead of asking a question. Human chatter goes to stderr;
only the JSON payload goes to stdout.

Exit codes are stable, so CI can branch on the failure class:

| Code | Meaning |
|-----:|---------|
| 0 | Success. |
| 1 | Generic error. |
| 3 | Run lock busy — a run is already in progress (409). |
| 4 | Residency refused — a local-disk action under remote residency. |
| 5 | Alpha locked — a gated build isn't activated (403). |
| 6 | Origin denied (403 `origin_not_authorized`). |
| 7 | Run ended in a failure-class `stop_reason`. |
| 8 | Not found. |
| 9 | Sidecar unreachable. |
| 10 | Foreign sidecar — the desktop app owns this store (see below). |
| 11 | Member-health preflight failed — a provider isn't ready. |
| 12 | Run setup required — confirm setup before the first run. |

Because `run` prints its terminal status *and* stamps a non-zero exit on a
failure-class `stop_reason` (code 7), `errorta run --json --yes` is safe to gate a
pipeline on.

### One sidecar, one owner

The CLI and the desktop app share the **same store on disk**, but exactly **one
sidecar** may own that store at a time. Whichever front-end starts first spawns
the sidecar; a CLI-spawned one advertises itself in `${ERRORTA_HOME}/sidecar.json`
so successive CLI invocations and multiple terminals **adopt** the same one
instead of starting a second.

The CLI upholds the single-instance contract by **refusing to spawn a second
sidecar** when it detects the desktop app (or a foreign sidecar) running against
the same store — for *every* command, reads included (exit code 10). A second
sidecar's crash recovery would flip the app's live run to `interrupted`, requeue
its tasks, and prune its worktrees, so this refusal is deliberate. Sequential
interchangeability still holds: drive a project in the app, close it, then drive
the same project from the CLI (or vice versa).

If both sides are up, either close the app, or give the CLI a separate store:

```bash
errorta --home ~/.errorta-cli status
```

Manage the CLI's own sidecar explicitly when needed:

```bash
errorta sidecar status
errorta sidecar restart
errorta sidecar stop
```

If the CLI and the running sidecar were built from different commits, the CLI
prints a compatibility warning (behavior may differ) — update so both sides match.

---

## Troubleshooting & FAQ

**`errorta: command not found`.** The binary or shell function isn't on your
`PATH`. For the single binary, `mv ./errorta /usr/local/bin/errorta`. For a
checkout, symlink the dev launcher:
`ln -sf "$PWD/scripts/dev-errorta" /usr/local/bin/errorta`.

**`run` says setup is required (exit code 12).** A fresh project must confirm the
readiness gate first: `errorta setup --confirm --yes`, then `errorta run --yes`.

**`run` says there are no members / an empty team.** You haven't applied a team.
Set one and commit it: `errorta team set dev <route>` then
`errorta team apply --yes`. Confirm with `errorta team show`.

**A provider shows `connected` as unknown.** The `connected` flag is
**fail-closed** — it stays unverified until a live probe succeeds. For a CLI
provider run `errorta connect claudecode cli` (which runs the vendor `/test`
probe); for an API provider re-run `errorta connect <provider> api`. Use
`--login` to print the vendor's login command if the CLI needs a sign-in first.

**A command exits with `origin_not_authorized` (exit code 6).** The sidecar
rejected the request origin. This shouldn't happen in normal use — it usually
means a mismatched or foreign sidecar; try `errorta sidecar restart`.

**The CLI refuses every command with a foreign-sidecar message (exit code 10).**
The desktop app is running against the same store. Close the app, or point the CLI
at a separate store with `--home ~/.errorta-cli`. See
[One sidecar, one owner](#one-sidecar-one-owner).

**`--json` returns an error instead of the data.** `--json` never prompts, so a
command that needs a confirmation must be given `--yes`, and one that needs a
provider or a running sidecar exits non-zero. Check the exit code and stderr — the
error body is machine-readable.

**Nothing streams while I watch.** Only read commands take `--watch`; `run`
already streams to completion on its own (adding `--watch` to it is a no-op the
CLI notes). If a `--watch` view looks idle, the run may simply be between turns —
`errorta status` confirms the current `state`.

**Where do my keys and projects live?** Under `ERRORTA_HOME` (defaults to
`~/.errorta`): keys in `provider-keys.json` (mode 0600, masked on read), projects
under `council/coding-projects/<id>/`. The CLI and the app read and write the same
files.

---

## See also

- `errorta --help` and `errorta <command> --help` for the full command list.
- `DEVELOPING.md` for the from-source setup.
- `docs/SIDECAR_LIFECYCLE.md` for how the embedded sidecar is spawned and shared.
