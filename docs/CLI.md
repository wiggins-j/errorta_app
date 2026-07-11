# Errorta headless CLI

`errorta` is the terminal front-end for the Errorta Coding Council. It drives the
same local engine the desktop app uses — spin up a coding team, watch it work,
steer it, and ship a PR — without leaving your shell. It is a thin **client** of
a local sidecar server; the CLI binary carries that server inside itself, so
there is nothing else to install or run.

- Everything is **local**. The CLI talks to a loopback sidecar; your code and
  keys never leave the machine (unless you configure a remote provider yourself).
- It shares the **same on-disk store** as the desktop app, so a project is
  interchangeable between the terminal and the GUI — one at a time (see
  [Sole owner](#sole-owner)).

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
errorta new my-project --repo .
```

Either way a `.errorta-project` pointer is written in the directory so future
`errorta` invocations from here (or the GUI) resolve to the same project.

---

## Run and watch

Kick off a run and stream it live:

```bash
errorta run --watch          # start the team, follow the live view
```

While watching, press `Ctrl-C` to detach (the run keeps going). Re-attach any
time by tailing a read command with `--watch`:

```bash
errorta log --watch          # the team log, live
errorta status --watch       # the run/task board, live
```

Read commands (no side effects) can always be re-run to get a snapshot:

```bash
errorta status               # current run + task board
errorta log                  # recent team-log entries
errorta prs                  # open pull requests
errorta tasks                # the task list
errorta turns                # per-turn history
```

---

## Verbosity — the control surface

The live view is **layered**. A single global level 0–5 unlocks a fixed set of
channels; you don't have to drown in output to see the one stream you care about.

| Level | Name       | Adds channels |
|------:|------------|---------------|
| 0     | `quiet`    | (headlines only) |
| 1     | `default`  | team-log, attention, prs |
| 2     | `verbose`  | + decisions, runtime (task transitions / test runs / launches) |
| 3     | `debug`    | + turns, tokens |
| 4     | `trace`    | + tools (tool events, prompt/response) |
| 5     | `firehose` | + poll, http (raw poll diffs / HTTP trace) |

Set it globally, per invocation or live in the REPL:

```bash
errorta run --watch -V verbose      # start at level 2
```

```text
/verbosity 3      # set the global level live
/watch tools      # force-show one channel without cranking the whole level
/mute prs         # force-hide a channel
/solo team-log    # show only this channel; /unsolo to clear
```

So you can sit at `default` and `/watch tokens` when you want cost detail, then
`/mute` it again — precise focus instead of an all-or-nothing firehose.

---

## Scripting with `--json`

Every command takes a global `--json` flag that prints the raw route payload to
stdout — stable, parseable, and free of any decorative rendering. Use it to wire
Errorta into scripts and CI:

```bash
errorta status --json | jq '.run.state'
errorta --json --no-spawn tasks        # never spawn a sidecar; error if none is up
```

`--json` is strictly non-interactive: it never prompts, never streams a live
view, and never prints the onboarding hint. A command that needs a provider (or
a running sidecar) simply exits non-zero with a machine-readable error instead of
asking a question. Exit codes are stable, so `errorta run --json --yes` is safe
to gate a pipeline on.

---

## Sole owner

For v1, **one owner per data store at a time.** Don't run the CLI and the desktop
app against the same store simultaneously — a second sidecar next to the first
can corrupt an in-flight run (its recovery sweep re-queues live work). The CLI
detects a running desktop app and refuses to start a second sidecar rather than
risk it.

If you need them side by side, give one of them a separate store:

```bash
errorta --home ~/.errorta-cli status
```

Manage the CLI's own sidecar explicitly when needed:

```bash
errorta sidecar status
errorta sidecar restart
errorta sidecar stop
```

---

## See also

- `errorta --help` and `errorta <command> --help` for the full command list.
- `DEVELOPING.md` for the from-source setup.
- `docs/SIDECAR_LIFECYCLE.md` for how the embedded sidecar is spawned and shared.
