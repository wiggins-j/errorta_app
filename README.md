# Errorta

> Assemble a team of coding agents — each on the model you choose — and
> ship real code. Built on the local-AI framework that admits when it's
> wrong and remembers your corrections.

Errorta is a polished desktop application ([Tauri 2](https://tauri.app) +
React + a Python/FastAPI sidecar over the [AIAR](https://github.com/wiggins-j/aiar)
framework). Its headline act is **multi-model coding teams**: a
PM-led team of coding agents — a project manager, programmers, and
reviewers, each backed by whatever model or subscription you point it at —
that plans a task queue, writes code on isolated branches, runs real
sandboxed tests, reviews its own diffs behind a merge gate, and delivers a
working MVP or a GitHub pull request. Around that, Errorta also runs
**multi-model council deliberations**, answers questions over **your own
documents with a verdict on every answer**, and can be **driven from an
iPhone on your own network**.

**Local-first, not local-only.** You can run everything on your machine —
local Ollama models, a local vector store, no network — and that's a fully
supported mode. But it's a choice, not a constraint: Errorta talks just as
happily to **Claude, OpenAI, Google, an SSH-remote or hosted AIAR, and your
existing Pro/Plus subscriptions**. Mix local and cloud however you like,
per room, per member, per coding agent — you decide where each call goes.

---

## Quick start — build & install on macOS

Errorta builds from source with the scripts already in this repo — no extra
setup code. On a Mac with the prerequisites below, a fresh clone becomes a
running app in one build command.

**Prerequisites:** [Node.js](https://nodejs.org/) ≥ 20 · [Rust](https://rustup.rs/)
(stable ≥ 1.77) · [Python](https://www.python.org/) ≥ 3.10 · Xcode Command
Line Tools (`xcode-select --install`).

```bash
# 1. Clone and enter the repo
git clone https://github.com/wiggins-j/errorta_app.git
cd errorta_app

# 2. Frontend dependencies
npm install

# 3. Python sidecar dependencies
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd ..

# 4. Build the app and install it to /Applications
bash scripts/rebuild-app.sh --install
```

Then launch **Errorta** from your Applications folder.

**First launch (unsigned build).** A from-source build isn't code-signed or
notarized, so macOS Gatekeeper blocks the first open. Right-click the app →
**Open**, or clear the quarantine flag:

```bash
xattr -dr com.apple.quarantine /Applications/Errorta.app
```

**Optional — local corpus grounding (AIAR).** `rebuild-app.sh` warns if the
AIAR framework isn't installed in the Python venv, but still builds. Cloud
and subscription models (Claude/OpenAI/Google/CLIs) work without it. To
enable local document grounding, clone [AIAR](https://github.com/wiggins-j/aiar)
alongside this repo and install it editable:

```bash
python/.venv/bin/pip install -e ../aiar
```

**Iterating without a full bundle.** For day-to-day development, skip the
bundle and run the two-terminal dev flow — the Python sidecar
(`python -m errorta_app.server`) in one terminal and `npm run tauri:dev` in
another. See [`DEVELOPING.md`](DEVELOPING.md) for the full local-run guide.

---

## The star — multi-model coding teams

Point Errorta at a goal (or an existing repo) and it stands up a **team of
coding agents**, each of which can run on a **different model or
subscription**, and drives them to shipped code.

- **PM-led agent team (F087).** A project-manager agent drives a task queue
  across programmer and reviewer agents: branch-per-task work in isolated
  git worktrees, real sandboxed test runs, structured diff review with a
  merge gate, PR-style merge to the project branch, persisted run recovery,
  and an accepted MVP exported to a user-facing folder. A post-`done`
  delivery review re-verifies the *integrated* head — a final reviewer pass,
  a final test run, and for a runnable program a headless launch probe — so
  "done" means the delivered code was actually reviewed, tested, and ran
  (F146).
- **One team, many models.** Put the PM on a strong model, the programmers
  on a fast/cheap pool, and a reviewer on a skeptical model — mixing
  Anthropic, OpenAI, Google, local Ollama, and Pro/Plus subscriptions in a
  single team. Model assignment is by role, and the PM can reassign as it
  learns (next-task effect, grounded in the models you can actually reach).
- **Bring your own repo (F135).** Import an existing project — clone from
  GitHub (`gh`-authed, no token in the URL) or register a local folder — and
  the PM *infers* a North Star + Definition of Done from the README and code
  for you to review and accept. A first-class "what to work on right now"
  directive scopes the team, and the result comes back as a GitHub pull
  request; nothing lands in your repo until you accept it.
- **Talk to the PM (F145).** Set up and steer the team in plain language. An
  **AI Wizard** turns one conversation into a fully-runnable project — a
  charter plus a team whose models are grounded in the providers you
  actually have, autonomy, and governance, all assumed sensibly when you
  don't spell them out. Once it's running, tell the PM "put the devs on
  Sonnet" or "go autonomous, don't ask me": model reassignment by role,
  autonomy, and governance each apply as a reviewable **PM Change** (Accept
  keeps / Decline reverts). Grounded-or-refuse — it won't invent a model it
  can't reach.
- **Token visibility (F143/F143-01).** Every turn's real input/output tokens
  are recorded per member and per project — genuine, never zero-filled:
  measured from the provider when it reports usage, otherwise estimated from
  Errorta's own prompt+response bytes (with honest `measured`/`estimated`
  provenance and a self-calibrating estimator), plus a per-member Context
  Report of what actually went into each prompt.
- **Reliability + supervision.** Member health signals (F120) surface a
  failing agent (logged-out CLI, missing binary, 401/429) as a blocking
  problem instead of looping silently; a run-readiness gate (F121)
  configures governance and caps before the first run; attention signals and
  a cross-project Director tier are specced (F117–F119).

---

## What else Errorta can do

### Council — many models deliberate, one answer comes out
- **Multi-model chat (F031).** Configure a "room" of members, pick a topology
  (parallel answers, round-robin, free council, moderator-led), and watch them
  deliberate to a finalized answer. A no-opinion **neutral leader-judge** (F080)
  can watch each round for early-stop and tie-break.
- **Context isolation as a trust feature.** Each member receives only the context
  its policy allows; a redacted-summary member provably gets different bytes than a
  full-context member in the same turn. The inspection drawer makes this visible.
- **Credibility mode (F078–F084).** Tool-backed research, peer credibility scoring,
  an entailment gate, adversarial/steelman roles, and verified final citations.
- **Live interjection (F049).** Send a message into a *running* Council run; the
  next member treats it as authoritative direction and steers mid-deliberation.
- **Room editor (F033/F075).** Build and edit rooms — members, providers, routes,
  context/transcript access, tool policy — without hand-editing JSON.

### Knowledge — your documents, answered with a verdict
- **Judge + grounding loop (F001).** Every answer comes with the model's structured
  verdict on whether it's any good. Accepted corrections persist and feed forward
  into future answers for the same prompt — semantically, via embedding-keyed
  grounding lookup (F024), so a correction also helps near-identical questions.
- **Judge depth.** Verdict-diff against your last run, a prior-verdict picker, a
  pass-rate chart, a latency histogram (p50/p95/p99), and **judge replay** that
  re-runs accepted verdicts across the grounding store to surface drift and wins.
- **Corpus management (F004, F114).** Drag-and-drop ingestion of PDF, DOCX, XLSX,
  PPTX, HTML, and plaintext; folder watch with auto-ingest (F005); corpus refresh
  with a before/after diff view (F015); delete-corpus; and a unified corpus catalog
  shared across Knowledge, Council, and the Coding Team (F095).
- **Brief-driven collection (F008).** Write a markdown brief, and an agent builds a
  corpus for you — arXiv / NASA NTRS / generic-HTML connectors, a compliance gate,
  resumable collection, edit history with diff/restore, and `.tar.gz` bundle
  export/import.

### Models — bring whatever you already have
- **Multi-provider gateway (F030/F034).** Anthropic, OpenAI, Google, local Ollama,
  and any OpenAI/Anthropic-compatible custom endpoint (LM Studio, vLLM, llama.cpp,
  Together, …). Keys live in `~/.errorta/provider-keys.json`, masked on read.
- **Subscription-backed providers (F040).** Use a Claude Pro/Max or ChatGPT plan as
  a council backend by shelling out to the official `claude`/`codex` CLIs — the
  vendor owns the OAuth; Errorta never sees the credential. Guided login launcher
  (F040-01) makes first-time setup something other than a terminal chore.
- **Agentic tool use (F039).** Council members can search the web (SearXNG),
  fetch pages (SSRF-guarded), and read/write/execute code inside a per-platform
  hardened sandbox (macOS seatbelt, Linux bubblewrap, Docker), with auto-apply
  patches reviewed and merged only on explicit human accept.

### Mobile — drive your desktop from your iPhone
- **On-device iOS companion (F065/F066/F070).** A dedicated TLS listener (off by
  default) serves a pinned, owner-approved pairing flow so a phone on the same
  Wi-Fi can view runs, control them, and clear the approval inbox. Freshly-paired
  devices are read-only until granted more from the desktop. Tailscale support
  (F071) extends this off-LAN.

### Headless CLI — drive the Coding Team from a terminal
- **`errorta` (F147).** A terminal front-end for the Coding Council: connect a
  provider, scope a project, start a run, and watch it live — no window needed.
  It ships as one self-contained binary that is both the CLI and its own embedded
  sidecar, shares the same on-disk store as the desktop app, and has a layered
  verbosity dial plus a `--json` surface for scripting. See
  [`docs/CLI.md`](docs/CLI.md).

### Platform
- **Tauri 2 desktop shell** (Rust) with a system tray (Show / Quit / check for
  updates, hide-on-close) and an auto-updater skeleton.
- **Hardware scan + model recommendation (F002)** and **Ollama detect / on-demand
  install (F003)** for a smooth first run.
- **Configurable data residency (F-INFRA-12).** Local, SSH-remote, or hosted —
  the active sidecar originates every model call and holds the keys.
- **Service API (F009-01/02).** Consent-gated device pairing, token-scoped
  `/services/*`, and fail-closed, AIAR-only responses so other local apps can use
  Errorta as their backend.
- **Diagnostics + safety.** Redacted local diagnostic bundles (F-INFRA-06), a debug
  logging toggle with a live log tab (F032), and an export-to-USB path (F010).

Two foundational feature designs are included as reference:
[judge + grounding loop](docs/specs/F001-judge-and-grounding-loop.md) and
[corpus drag-and-drop](docs/specs/F004-corpus-drag-and-drop.md).

---

## What this repo is

- The **source for Errorta**, the desktop product.
- Built on top of [AIAR](https://github.com/wiggins-j/aiar) — our free, open-source
  local-AI framework. AIAR provides the substrate (RAG pipeline, LLM-as-judge,
  grounding store, retrieval primitives). Errorta provides the product: the desktop
  shell, the Coding Team and Council orchestration, the multi-provider gateway, the
  mobile companion, and the polished UX.

## What this repo is **not**

- Not the framework itself. That lives at [`wiggins-j/aiar`](https://github.com/wiggins-j/aiar) — free, open-source, Apache-2.0.
- Not the public website / download site. That lives in a separate repo (`errorta-downloads`).

> ### For contributors — Errorta uses the real AIAR, not a mock
>
> **AIAR is the framework Errorta is built on, and it can run as a live service.**
> Errorta integrates against a **real AIAR** instance — local in-process, or a
> deployed AIAR server reached via `remote-aiar.json` — in dev and in production.
>
> Do not treat AIAR as a far-off third-party dependency to stub around or
> "wait on." When AIAR is reachable, wire to it and **validate against the live
> server**. The `FakeAiar` test double (`python/tests/fakes/fake_aiar.py`) exists
> for one reason only — **hermetic, offline unit tests** that must not touch the
> network or mutate real corpus state. It is never the product path. See
> `python/scripts/validate_f096_retrieve_live.py` for the live check.

## Read these first

- [`docs/NORTH_STAR.md`](docs/NORTH_STAR.md) — product identity, who it's for, what makes it different, what we're explicitly NOT building.
- [`docs/AIAR_SETUP.md`](docs/AIAR_SETUP.md) — what AIAR is, local vs. remote, and how to connect it. First run just connects your models; AIAR/knowledge is set up in Settings.
- [`DEVELOPING.md`](DEVELOPING.md) — how to actually run the thing locally.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute.
- [`docs/SIDECAR_LIFECYCLE.md`](docs/SIDECAR_LIFECYCLE.md) and [`docs/SYSTEM_TRAY.md`](docs/SYSTEM_TRAY.md) — architecture notes.

## Built on AIAR

Errorta is the polished product layer on top of [AIAR](https://github.com/wiggins-j/aiar).
When Errorta ships, AIAR ships alongside it. The framework is free and open; anyone
who wants to roll their own UI on top of AIAR can. Errorta is just the one we want to use.
