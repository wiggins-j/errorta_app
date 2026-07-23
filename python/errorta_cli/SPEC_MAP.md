# Spec coordinate map

Docstrings and comments in `errorta_cli` cite external coordinates ("F147 §4.2",
"S9b", "golden invariant #6", "review LOW-3", "F100"). Those are meaningful in the
Errorta design docs but opaque in isolation. This table decodes the coordinates
that actually appear in this package so a reader can follow a comment without the
spec open. It is a **reference key, not a spec** — the authoritative rationale
lives inline next to the code it explains; this file only translates the labels.

When you add a new coordinate to a comment, add a one-line entry here too.

---

## F147 — the master CLI feature/plan

**F147** is the plan this whole package implements: the headless `errorta`
terminal front-end as a **pure sidecar client**. Every capability is an existing
loopback HTTP route on the Errorta Python sidecar; the CLI never re-implements
engine logic and owns exactly one sidecar per `ERRORTA_HOME`. "The plan" / "plan
§N" refers to sections of the F147 plan (distinct from "spec §N", the F147 spec).

### F147 spec sections (`§N` / `§N.M`)

| Coordinate | Refers to |
|---|---|
| `§2` | Run stop-reasons and the stable exit-code contract |
| `§4` | The HTTP client / transport layer |
| `§4.1` | The `httpx` sidecar client + origin header |
| `§4.2` | Sidecar transport: typed error mapping and lifecycle semantics |
| `§4.3` | `ERRORTA_HOME` / on-disk path resolution |
| `§4.4` | Background polling → synthetic client-side events (no SSE) |
| `§5.1` | Config + project↔directory mapping (`.errorta-project` pointer / repo match) |
| `§5.2` | The command registry (single source both front-ends dispatch through) |
| `§5.3` | Typed CLI errors + the exit-code map |
| `§6` | Layered verbosity — the first-class control surface |
| `§6.1` | The channel → minimum-global-level table |
| `§7` | First-run onboarding |
| `§7.1` | Provider `connect` (writing the same store the app uses) |
| `§7.2` | Team assembly (CLI-local draft + `team apply`) |
| `§7.3` | The conversational AI setup `wizard` |
| `§8` | Command groups (the mutating/reading command surface) |
| `§8.1` | Project lifecycle commands |
| `§8.2` | Run control (`setup` / `run` / `cancel` / `resume` / `continue`) |
| `§8.3` | `focus` / north-star steering |
| `§8.5` | Runtime control sub-actions |
| `§8.6` | `publish` |
| `§9` | The render layer (field-selecting renderers) |
| `§11` | Distribution — the self-contained multicall binary (`__serve__`) |
| `§12` | Run `continue` at a governance gate |
| `§13.1` | Single-instance sidecar lifecycle |
| `§14` | Provider-key handling — the load-bearing safety property |
| `§18` | Sidecar discovery / adoption (internal spec ref) |

## S-stages (`Sn` / `Sna` / `Snb`)

The CLI was built in slices; an `Sn` tag marks the slice/command-group a piece of
code belongs to (see the ordered list in `registry.py`).

| Coordinate | Refers to |
|---|---|
| `S1` | The interactive REPL / slash-command shell (minimal tokenizer, completion) |
| `S2` | Read commands (`status`, `log`, `decisions`, `tasks`, `prs`, `tokens`, `turns`, `attention`, `runtime`, …) |
| `S3` | Run-control mutations (`setup` / `run` / `cancel` / `resume` / `continue`) |
| `S4` | Provider onboarding + conversational setup (`connect`, `wizard`) |
| `S5` | Project lifecycle (`new`/`import`/`projects`/`open`/`switch`/`delete`) + north-star / focus |
| `S6` | Mid-run steering + file/worktree edit/accept (`interject`, `task`, `files`, `pm`, `governance`) |
| `S7` | `publish` + `grounding` + test-command config |
| `S9` | Shared-sidecar co-drive (GUI + CLI on one store) — the umbrella slice |
| `S9a` | Origin allowlist + cross-process run lock / owner-pid coordination |
| `S9b` | One shared sidecar adopted and co-driven by both the app and the CLI |

## Other feature specs (`Fxxx`)

| Coordinate | Refers to |
|---|---|
| `F049` | The "pinned directive" contract the PM consumes on its next plan turn |
| `F100` | Governance gates — a run can stop *at* a gate and be continued |
| `F137` | Current Focus (north-star steering) |
| `F143` | Token-usage rollup (`by_member` / `by_route` / `by_role` / `total`) |
| `F145` | The AI Wizard + grounded control-actions |
| `F146` | Delivery outcome (the accept/delivered marker on the project payload) |
| `F149` | Shell integration — the auto-`cd` hook |
| `F150` | The team builder (`create` / `add` + `--default`) |
| `F151` | Command aliases + `--watch` render mode (snapshot vs stream) |
| `F158` | Commands with both tail-able and snapshot sub-verbs + their stream hooks |

## Golden invariants (`golden invariant #n`)

Cross-cutting properties of the CLI, each locked by a test (named where known).
The description is the property; the code comment says why it holds locally.

| Coordinate | Property | Test |
|---|---|---|
| `#1` | Client-only: `errorta_cli` imports nothing from `errorta_app` (except `serve.py`'s in-process sidecar launch) | `test_import_boundary` |
| `#2` | Trusted origin: every sidecar request carries the static `x-errorta-origin` header | `test_client_origin` |
| `#3` | Front-end parity: argv `errorta <cmd>` and REPL `/<cmd>` dispatch through the one registry identically; onboarding never blocks `--json` | `test_registry_parity` |
| `#4` | Key/secret safety: an API key/token is never an argv arg, never logged or rendered, never handed to a foreign sidecar | `test_connect_key_never_leaks` / `test_connect_team_wizard` |
| `#5` | No raw leak: renderers *select* the fields they surface — raw payloads / secrets never reach stdout | `test_render_no_raw_leak` |
| `#6` | Own-run steering: the sole-owner guard refuses only a *foreign* desktop app, never the CLI's own live run |  |
| `#7` | Confirmation gate: a run-starting / steering mutation never fires without an explicit yes (`--yes` when non-interactive or `--json`) |  |

(A couple of run-control comments tag the sole-owner guard itself with a
golden-invariant number; the guard's behavior is described under `#6`/`#7` above.)

## Review / remediation tags

| Coordinate | Refers to |
|---|---|
| `Rn` (e.g. `R1`, `R3`, `R5`) | A CLI-review remediation round from `docs/CLI_REVIEW.md` (e.g. R3 = bearer-token auth, R5 = comment hygiene) |
| `review LOW-n` / `LOW-n`, `MED-n`, `HIGH-n` | A numbered finding from a CLI code review (severity + index) |
| `S3 review #3` | A numbered finding in the S3 (run-control) review |

## Not spec coordinates

These look similar but are **tooling** codes — do not confuse them with the above:

| Token | Actually |
|---|---|
| `# noqa: S603` | Ruff/bandit rule S603 (subprocess call) — suppressed with an inline reason |
| `# noqa: S324` | Ruff/bandit rule S324 (insecure hash) — suppressed (cursor id, not security) |
