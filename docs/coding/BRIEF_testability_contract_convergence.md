# Architect brief — the testability-contract convergence problem

You are a solutions architect joining the errorta project cold. Your job is to
independently diagnose a recurring failure pattern and propose your own architecture
for it. This brief gives you the problem, the evidence, and where to look. **It
deliberately contains no proposed solution — do not expect one, and do not anchor on
anything here as a hint toward a fix. Form your own hypotheses and design.**

## What errorta is (minimum context)

errorta runs an autonomous "coding council": a team of LLM agents (a PM, several
developers, a reviewer, a tester — in the runs below, all the same model) that build
a software project from a North Star + a Definition of Done, opening/reviewing/
merging PRs against an internal git tree, with no human in the loop. A run ends when
the PM's `done` claim is accepted (`stop_reason=definition_of_done`) or when a safety
detector stops it (e.g. `no_progress`, `revise_livelock`, `completion_blocked`).

To judge web deliverables the engine runs a headless-Chromium **web:probe** after
merges. Recent specs added a **behavioral/testability contract**: for a game that
declares a load-bearing mechanic, the delivered artifact is expected to expose a
scriptable hook (a `window.__probe` object) so the probe can drive the game
headlessly and verify the mechanic actually affects outcomes. The probe folds several
signals (renders / responds to input / mechanic has effect) into a pass/fail that
gates delivery and `done`.

## The observed problem

The council can now build a genuinely good game — but across multiple runs it **fails
to converge to a shipped result specifically around the testability contract**, even
when the underlying game is fine. The symptom recurs in different forms:

- **Run 3 (gravity-golf-3):** stopped `no_progress`. The game rendered, was
  human-playable, and gravity genuinely worked, but the probe's interaction check
  stayed red the whole run; the council churned 7+ PRs chasing it and stalled.
- **Run 4 (gravity-golf-4):** stopped `revise_livelock` after 22 merges. Direct
  simulation of the delivered physics shows gravity is genuinely non-inert (a
  straight shot deflects ~155px and misses; without the wells it sinks) and the game
  is human-playable and renders clean — i.e. the *game* substantially meets the DoD.
  Yet the run never shipped: late revisions oscillated around the probe hook /
  mechanic-toggle, a "green anchor" regressed, and the livelock detector stopped it.

The engineer who built the recent probe/contract specs believes this contract is now
the dominant source of council churn. **Treat that as a claim to verify or refute,
not as established fact.** Your first task is to determine what is actually happening
and why — including questioning whether the framing ("testability-contract
convergence problem") is even the right one.

Open questions you are expected to answer in your own terms (this list is
descriptive, not prescriptive — add or discard as your investigation warrants):

- Is the recurring cause really the testability contract, or something upstream/
  adjacent (the probe, the review loop, the detectors, the DoD wording, the council's
  own reasoning, the git/revise machinery)?
- Why does the council converge on building the game but not on satisfying the
  contract? What is different about that class of work?
- What, structurally, turns a good artifact + a contract requirement into an
  oscillation/stall rather than a clean pass?
- Is any of this a defect in the game the council builds, a defect in how the engine
  measures it, a defect in how the council is steered, or something else?

## Where to look (all read-only; the runs are stopped)

Engine source (the mechanism):
- `python/errorta_council/coding/web_probe.py` — the probe verdict, the hook contract
  string, the mechanic/interaction fold.
- `scripts/web-probe.mjs` — the headless probe itself (what it drives and measures).
- `python/errorta_council/coding/runner.py` — the run loop, PR review/revise path,
  delivery/done gating.
- `python/errorta_council/coding/autonomy.py` — the detectors and their limits
  (`no_progress`, `revise_livelock`, `completion_refused`, etc.).
- `python/errorta_council/coding/{completion.py,gate_state.py}` — done gating + the
  read-only gate view.

History of prior attempts (read as context on what has already been tried and why —
NOT as a menu of solutions): `docs/specs/SPEC-30, 34, 35, 36, 37, 38, 39*.md`.

Run evidence (the ground truth — trust the ledgers/artifacts, not any summary):
- Ledgers: `~/.errorta/council/coding-projects/gravity-golf-{2,3,4}/` —
  `decisions.jsonl` (every choice, incl. the stop reason and the review findings),
  `prs.json` (per-PR probe fields: `probe_interaction_changed`, `probe_mechanic_ok`,
  `probe_passed`, `review_findings`), `turns.jsonl`, `run_state.json`.
- Delivered code: `~/.errorta/council/apply-workspaces/coding-gravity-golf-{2,3,4}/`
  (the actual game each run produced).
- To reproduce a probe verdict yourself: serve a delivered tree
  (`python3 -m http.server <port> --directory <tree>`) and run
  `node scripts/web-probe.mjs http://127.0.0.1:<port>/index.html`.
- To ground-truth the mechanic independently of the probe: import the delivered
  `physics.js` and simulate a shot directly (don't rely on the probe or the council's
  self-report for whether the game is actually good).

Compare the runs: gravity-golf-2 shipped an *inert* game (a different, earlier
failure); 3 and 4 are the convergence cases above. The contrast between "shipped but
bad" and "good but wouldn't ship" is likely informative.

## What we need from you

1. A verified root-cause account of the recurring pattern, grounded in specific
   file/line/decision/PR evidence — including whether the "testability contract"
   framing holds.
2. Your own proposed architecture/direction to resolve it. It is yours to invent; do
   not feel bound by the shape of the existing specs, the probe, or the hook. If the
   right answer is to change, replace, or remove part of the current mechanism, say
   so and justify it. If the right answer is elsewhere entirely, say that.
3. Call out the trade-offs and what you'd need to validate before committing.

Ground rules: verify every claim against the logs/code; the runs are stopped so all
reads are safe; do not trust any agent's or PM's self-report over the recorded
artifacts. Come to your own conclusions.
