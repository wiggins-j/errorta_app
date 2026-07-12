# F149 — `errorta new <id> [location]`: location argument + auto-`cd` shell hook

**Target version:** v0.1 (CLI)
**Status:** proposed
**Owner:** wiggins-j

> Feature number is provisional — confirm against the F-registry before merge.

---

## Problem

Two friction points hit a new user immediately after `errorta new`:

1. **You can't choose where the project lands in one obvious way.** Today you either
   pass `--here` (deliver into the current dir) or `--delivery-root PATH`, and **the
   target directory must already exist**. A newcomer typing
   `errorta new reddit-clone ~/dev/reddit` reasonably expects that to just work —
   creating the folder if it isn't there yet.
2. **Your shell stays in the old directory.** The very next commands
   (`errorta team set …`, `errorta run`) act on the bound project, so sitting in the
   wrong directory is confusing. The user expects to be *moved into* the new project.

A CLI binary **cannot change its parent shell's working directory** — a child
process `chdir` never propagates up to the shell that launched it. So #2 can't be
solved by the binary alone; it requires an opt-in **shell integration hook**.

## Goals

- `errorta new <id> [location]` accepts an optional positional `location`.
- If `location` doesn't exist, create it (`mkdir -p`) — inside the existing
  delivery-root safety guards.
- If no `location` is given, use the **same default the desktop app uses**:
  `~/Errorta Projects` (project delivered to `~/Errorta Projects/<id>`).
- After a successful `new`, an **opt-in shell hook** moves the terminal into the new
  project directory, so `errorta team set …` runs in the right place with no manual
  `cd`.

## Non-goals

- Auto-`cd` **without** the shell hook installed — impossible for a binary; documented
  as such, with a graceful hint.
- Changing the desktop app's project-location behavior.
- fish / PowerShell hooks in v1 (zsh + bash first; fish is a follow-up).

## Behavior / acceptance criteria

### The `location` argument
- `errorta new <id>` (no location) → project delivered under **`~/Errorta Projects/<id>`**
  (the existing server default — unchanged).
- `errorta new <id> <location>` → `location` is the **delivery root (parent)**; the
  project is delivered to **`<location>/<id>`**. *(Mirrors the app: a default root plus
  an `<id>` folder. The project folder name is always the id, so it's predictable.)*
- `location` may be **relative** (resolved against `$PWD`) or absolute; a leading `~`
  is expanded.
- If `location` does not exist, it is created with `mkdir -p` **before** the create
  request, subject to the guards below. Parent-creation failure → non-zero exit with a
  clear message; **no project is created**.
- `--here` remains supported and means `location = $PWD`. `location` and `--here` are
  **mutually exclusive**; supplying both errors.
- The existing `--delivery-root PATH` flag stays as an explicit alias for `location`
  (identical effect). If both the positional and the flag are given and **disagree**,
  error rather than silently pick one.
- The binding pointer (`.errorta-project`) is written **into the new project directory**
  (`<location>/<id>`), and the session is switched to that project — so a shell that
  ends up there is correctly bound.

### Safety guards (reuse the existing delivery-root validation)
Auto-`mkdir` must refuse exactly the targets the server already rejects for a delivery
root (`_validate_delivery_root` in `routes/coding.py`): the home directory itself, any
path inside a hidden home dir (`~/.something`), `~/.ssh`, the Errorta home, `/etc`, etc.
On refusal: non-zero exit, **create nothing**, name the reason. The guard is the single
source of truth — the CLI must not diverge from it.

### The auto-`cd` shell hook
- New command **`errorta shell-init <zsh|bash>`** prints a snippet the user adds once to
  their rc file:
  ```sh
  eval "$(errorta shell-init zsh)"    # in ~/.zshrc   (or: bash → ~/.bashrc)
  ```
- The snippet defines a wrapper function `errorta()` that:
  1. allocates a temp handshake file and exports `ERRORTA_CD_FILE` pointing at it,
  2. runs the real binary — `command errorta "$@"` — so the user sees normal output,
  3. if the binary wrote a directory path into `ERRORTA_CD_FILE`, does
     `builtin cd -- "$dir"` (quoted — the default path contains a space),
  4. removes the temp file and **preserves the binary's exit code**.
- The binary, on any command that establishes/changes the active project directory
  (`new` first; also `open` / `switch` / `import`), writes the resolved bound directory
  to `$ERRORTA_CD_FILE` **iff that env var is set**. When it's unset (no hook installed),
  nothing is written and nothing `cd`s — behavior is byte-for-byte unchanged.
- **Without the hook installed:** `errorta new` still creates + binds the project
  correctly; it just prints the destination path and a one-time hint,
  e.g. `tip: add  eval "$(errorta shell-init zsh)"  to ~/.zshrc to jump into new projects automatically`.

## UX flow

```sh
# one-time, in ~/.zshrc
eval "$(errorta shell-init zsh)"

# then, from anywhere:
errorta new reddit-clone ~/dev            # creates ~/dev (if missing) + ~/dev/reddit-clone,
                                          # binds it, and drops you INTO ~/dev/reddit-clone
errorta team set dev claude_cli.sonnet    # runs in the right directory — no manual cd
errorta team apply --yes
errorta run --yes
```

Default-location variant:

```sh
errorta new reddit-clone                  # ~/Errorta Projects/reddit-clone, and cd's there
```

## Implementation notes

- **CLI — `errorta_cli/commands/project.py`**
  - Add a positional `location` `Param` to the `new` command. The registry already
    fills bare tokens into non-flag params in order (`registry.py:117,137`), so ordering
    matters: keep `id` first and `location` second, ahead of the other value params, or
    parse it explicitly to avoid colliding with `north-star`/`dod` positionally.
  - Resolve one `delivery_root` from `location` / `--here` / `--delivery-root`
    (mutual-exclusion + disagreement checks), expand `~`, make it absolute, then
    guarded `mkdir -p` **before** `POST /coding/projects`.
  - `_write_binding` currently writes the pointer to `bind_cwd()`; for `new` pass
    `directory=<project dir>` so the pointer lands in the new folder.
  - After `switch_project` + pointer write, call a small helper `emit_cd_target(path)`
    that appends the absolute bound dir to `$ERRORTA_CD_FILE` when set. Reuse it in
    `open`/`switch`/`import`.
- **shell-init** — new `errorta_cli/commands/shellinit.py` (or a Typer sub-app) emitting
  the zsh/bash function. One function serves both shells; keep it tiny and POSIX-ish.
- **server — `routes/coding.py`**: no default change (`~/Errorta Projects` already the
  default). Prefer doing `mkdir -p` **CLI-side** but validating against the server's
  `_validate_delivery_root` rules so the two never drift; alternatively expose the guard
  as a shared helper.

## Edge cases

- `location` exists but is a **file** → error.
- `location` (or `<location>/<id>`) already contains a project / `.errorta-project` →
  refuse with a message pointing at `errorta open`, unless `--yes`/`--force` (decision:
  **refuse** by default).
- Relative `location` + auto-cd → resolve to absolute **before** writing the cd target.
- Permission denied on `mkdir` → non-zero exit, nothing created, nothing cd'd.
- `ERRORTA_CD_FILE` set but the command errors before binding → **don't** write a target
  (no spurious cd).
- Spaces in the default path (`~/Errorta Projects`) → the emitted function must quote
  (`cd -- "$dir"`).
- Hook installed but binary is an old version that ignores `ERRORTA_CD_FILE` → file stays
  empty, function no-ops. Forward/backward compatible.

## Testing

- **CLI unit:** positional-vs-flag precedence + disagreement error; `--here`↔`location`
  mutual exclusion; `~` expansion; guarded `mkdir -p`; guard refusals (home / hidden-home
  / `~/.ssh` / errorta-home); pointer written into the new dir; `$ERRORTA_CD_FILE`
  written **only** when set **and only** on success.
- **shell-init:** `errorta shell-init zsh|bash` output parses (`zsh -n`, `bash -n`); an
  integration test sources it, runs `new` under a tmp `HOME`/`ERRORTA_HOME`, and asserts
  `$PWD` moved into the project dir and `$?` is preserved.
- **Backwards-compat:** `--here` and `--delivery-root` behavior unchanged; with no hook
  installed, `new` output/exit are identical to today.

## Out of scope / follow-ups

- fish + PowerShell shell hooks.
- Auto-`cd` for a bare `errorta open <id>` invoked from an arbitrary directory (the same
  `ERRORTA_CD_FILE` handshake covers it — can ship right after `new`).
- A `errorta shell-init --check` doctor that verifies the hook is active.
