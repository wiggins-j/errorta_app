# Spec 14 — Ground the reviewer

**Source:** `docs/coding/RUN_ANALYSIS_GRAVITY_GOLF_2026-07-24.md` §6 **S3 (P0)**
**Target version:** v0.1 (engine)
**Status:** implemented, except **Item 6 — WITHDRAWN 2026-08-06** (the
`review_screenshot` knob and its serialization are deleted; see Item 6 for why)
**Owner:** wiggins-j

> Items 5 and 6 depend on [Spec 12](SPEC-12-in-loop-acceptance-gate.md); Items
> 1–4 stand alone and should land first.
>
> **Δ review** corrected two mechanics: Item 3's downgrade could not have stopped
> the spiral (the rejection branch keys on `approved`, not on findings), and
> Item 4's `num_turns` has no existing return channel from the provider. Item 2
> also gave up ownership of the reviewer `tool_guidance` segment to
> [Spec 17](SPEC-17-prompt-tool-catalog-coherence.md) to keep the two branches
> disjoint.

---

## Problem

Review in the gravity-golf run carried no information.

11 of 13 reviews returned in **5.5–8.7 seconds** with `approved: true, findings:
[]` — byte-identical in shape. Among them: the rendering module, which the
reviewer had been explicitly told to hold to a premium visual bar. It approved
"premium visual rendering" without ever seeing a pixel; the delivered result is a
flat gradient with a dead right third of the canvas, a `"Level 1: Level"` HUD
string reading a field that does not exist, and a black screen on first load.

When the reviewer finally *did* reject, it demanded execution evidence — *"no
evidence that the tests were actually run… no strokes-per-level table"* — which
no role could produce (see [Spec 12](SPEC-12-in-loop-acceptance-gate.md)). Those
rejections drove the revise spiral.

And one review turn (`t-690c64b3c890`, 40.5s) is the tell: the model emitted
Claude-Code-style `<function_calls>` Read/Glob invocations **as literal XML text**
in its response, because the reviewer's CLI invocation exposes no tools. The
model knew exactly what it needed — to read the repo — and the harness had no
way to let it.

## Why the capability isn't already there

It nearly is. Spec 11 (P1a) built the exact mechanism, and scoped it to DEV
turns only:

- `async_claude_cli.py:64-99` — when
  `request.extra["metadata"]["dev_repo_read_root"]` is a real directory, the
  provider runs the CLI with `cwd` = that worktree and `--tools "Read,Grep,Glob"`
  (`_DEV_REPO_READ_TOOLS`), `--max-turns 16` (`_DEV_REPO_READ_MAX_TURNS`), with
  the plain no-tools call retained as a fallback if the retrieval turn burns its
  budget without emitting an envelope (`:355-380`). The comment records the
  verification that this allowlist exposes *exactly* `{Read,Grep,Glob}` — no
  write, no exec, no network.
- `_dev_repo_read_root` (`async_claude_cli.py:180-196`) reads the key.
- The runner sets it in exactly one place: the DEV dispatch
  (`runner.py:3248-3261`), forwarded through gateway metadata at
  `runner.py:4118-4125`.

Nothing about the mechanism is dev-specific except the key's name and the two
call sites. The reviewer gets `_review_project_context` (`runner.py:2018-2061`)
— North Star, DoD, the merged file list, the changed-file list — plus a diff
capped at `_REVIEW_DIFF_CAP = 48000` (`runner.py:1623`) in its prompt, and no way
to look at anything else.

Two more gaps make the empty verdicts unfalsifiable:

- **Nothing distinguishes a considered approval from a reflex.** A 6-second
  `findings: []` and a 40-second grounded approval are recorded identically
  (`runner.py:3427-3442`).
- **Findings carry an optional `path` and no line** (severity/title/body/path,
  `runner.py:3424-3430`), so a finding need not point at anything. That is what
  lets "no strokes table" — a demand about the world, not about the diff — pass
  as a blocking finding.

## Goals

- The reviewer can **read the tree it is judging**, using the same read-only
  CLI mechanism the dev already gets, so its tool hunger becomes capability
  instead of XML leakage.
- An approval or a blocking finding must **cite evidence** — a file, ideally
  `file:line` — that exists in the tree under review.
- A verdict produced with **no retrieval and implausible speed** is treated as
  unparsed and retried once, then recorded as ungrounded rather than silently
  trusted.
- Once [Spec 12](SPEC-12-in-loop-acceptance-gate.md) lands, the reviewer sees the
  **latest gate output**. (This goal originally also promised a **screenshot** of
  the running artifact for a visual DoD — that half is withdrawn; see Item 6. The
  web probe's verdict on those pixels reaches the reviewer as text in the same
  gate output.)

## Non-goals

- Not giving the reviewer write or execute tools. Read-only stays read-only —
  the enforcement is at the CLI tool-availability layer, exactly as Spec 11
  established, and the same "if `--tools` semantics ever change, this must not
  ship" rule applies verbatim.
- Not replacing the diff in the prompt. Retrieval is **additive**; a reviewer
  that reads nothing must still produce today's verdict.
- Not a second review pass or a reviewer panel.
- Not vendor-general. Like Spec 11, only `claude_cli` honors retrieval today;
  codex/cursor are a documented follow-up and fall back to the unchanged path.

---

## Item 1 — Generalize the retrieval mechanism beyond DEV

**Design.** Rename the metadata contract from dev-specific to role-neutral:

- `async_claude_cli.py`: accept `metadata["repo_read_root"]`, keeping
  `dev_repo_read_root` as an accepted alias so a mixed-version sidecar/gateway
  pair keeps working. Rename `_dev_repo_read_root` → `_repo_read_root`,
  `_DEV_REPO_READ_TOOLS` → `_REPO_READ_TOOLS`, `_DEV_REPO_READ_MAX_TURNS` →
  `_REPO_READ_MAX_TURNS` (values unchanged: `"Read,Grep,Glob"`, `16`).
- `runner.py:4118-4125`: forward whichever key the member carries.
- `autonomy.py`: `reviewer_repo_read: bool` next to `dev_repo_read` (`:154`),
  round-tripped in `policy_to_dict` / `policy_from_dict` (`:157/183`), threaded
  into `build_run_turn` alongside `dev_repo_read` (`runner.py:2427`, `:4287`).
  **Δ review: the policy field itself lands in the batch's shared prep PR**, not
  on this branch — four specs otherwise edit the same ten lines.

**Note (coherence defect — owned by
[Spec 17](SPEC-17-prompt-tool-catalog-coherence.md) Item 2, decided in the prep
PR):** `dev_repo_read`'s dataclass default is `False` (`autonomy.py:154`) while
its docstring says *"Default ON"* (`:151`), `policy_from_dict`'s comment says
*"dataclass default (True)"* (`:230-233`), and `runner.py:2434` says *"(default
True)"* — four statements, two values. **Δ review: this must be decided before
either branch starts**, because `reviewer_repo_read` has to match it; if it is
settled inside Spec 17's branch while this branch assumes the other value, the
flagship capability lands half-on and both specs' acceptance criteria still pass.

**Acceptance.** A member carrying `repo_read_root` gets the read-only retrieval
invocation; one carrying only the legacy `dev_repo_read_root` still does. Neither
key → the plain single-shot call, byte-identical to today.

## Item 2 — Reviewer turns run with the PR tree mounted

**Design.** At the reviewer dispatch (`runner.py:3395-3410`) and the strict-mode
PM PR-review dispatch (`runner.py:3599-3612`), when `reviewer_repo_read` is on,
tag a **per-turn shallow copy** of the member (never mutate the shared config —
same pattern as `runner.py:3257-3261`) with:

```
repo_read_root = workspace.task_root(pr["task_id"], branch=pr["branch"])
```

The **PR's own worktree**, not master: that is the tree the verdict is about, and
it lets the reviewer open a file the diff only shows a hunk of. The merged
surface it must stay consistent with is already in the prompt via
`_review_project_context` (`runner.py:2018-2061`).

Best-effort and fail-open: any failure resolving the root falls back to the
unchanged member. Retrieval must never turn a turn that would have succeeded into
a member failure — the empty-result fallback in the provider
(`async_claude_cli.py:365-380`) is the second line of defence.

The reviewer prompt needs a `tool_guidance` sentence stating that it can read any
file in this worktree with Read/Grep/Glob and should open the files the diff
touches before approving. **Δ review: that segment is owned by
[Spec 17](SPEC-17-prompt-tool-catalog-coherence.md), not by this spec** — both
originally proposed adding the reviewer's first `tool_guidance` segment, which is
a duplicated feature across two engineers' branches. Spec 17 renders it from the
capability catalog with `repo_read=policy.reviewer_repo_read`, so it is correct
whichever way the flag is set, and the two branches stay disjoint.

**Acceptance.** With the policy on, a reviewer turn's provider invocation has
cwd = the PR worktree and tools = `Read,Grep,Glob`; with it off, the invocation
is byte-identical to today. A worktree-resolution failure degrades silently to
the plain path. No prompt-segment change belongs to this item.

## Item 3 — Findings and approvals must cite evidence

**Δ review — the original design could not have worked.** It said an uncited
blocking finding is "downgraded to `major` and does not by itself trigger the
rejection branch". But the rejection branch keys on the **verdict flag**, not on
findings: `approved = bool(parsed.intent.approved)` (`runner.py:3427`), then the
`else:` arm sets `changes_requested` and spawns the `revise:` task
(`runner.py:3455-3485`). Downgrading a finding changes only the text carried into
that task's detail; the PR still goes back and the spiral is untouched. The only
way a downgrade could stop it is by flipping `approved` to `True` — turning a
reviewer's explicit "do not merge" into a merge, which breaks the merge-gate
fail-closed invariant. Corrected below.

**Design.** Three changes, leaving the schema's `path` optional so an uncited
verdict is a *quality* signal, not a parse failure:

1. **Findings are annotated, never re-scored.** At the verdict boundary
   (`runner.py:3411-3442`), a `blocking` finding whose `path` is empty or names a
   file not in `workspace.changed_paths(branch)` ∪ master is marked
   `cited: false`. Severity is untouched, `approved` is untouched, and the PR
   still goes `changes_requested` — nothing auto-merges.
2. **An all-uncited rejection does not spawn a DEV `revise:` task.** In the
   shared rejection seam (`runner.py:3455-3485`), when every blocking finding is
   `cited: false`, the revise is suppressed and the verdict is routed instead —
   to a re-review or a PM escalation. **This is the same suppression
   [Spec 15](SPEC-15-capability-aware-planning.md) Item 3 performs from the
   capability side, and the two must not both implement it.** Ownership: **Spec
   15 owns the suppression and the routing** (it already owns the classifier and
   the escalation path); this spec owns only producing the `cited` flag that
   Spec 15's classifier reads. That keeps the shared branch region to one writer
   per side of the seam.
3. **Approvals record their grounding.** Persist on the PR record (`update_pr`,
   `runner.py:1047`) the retrieval evidence from Item 4: whether retrieval ran,
   turns used, wall time. This is what makes a rubber stamp visible in `errorta
   prs` after the fact instead of only in a post-mortem.

**Acceptance.** An uncited blocking finding is flagged `cited: false`, `approved`
is unchanged, and the PR is `changes_requested` (never auto-mergeable) — the
fail-closed lock. A finding citing a changed file is `cited: true` and behaves
exactly as today. An approval carries retrieval evidence on the PR record. A
verdict with only non-blocking findings is unaffected.

## Item 4 — A sub-floor, no-retrieval verdict is treated as unparsed

**Design.** The primary signal is **retrieval turns**, not latency: the CLI
already returns `num_turns` in its result object (read today only for a log line,
`async_claude_cli.py:370-378`).

**Δ review — name the whole seam; it is four files, not two.** `MemberCaller` is
`Callable[[dict, str], str]` (`runner.py:62`) and returns only a string. The one
existing side-channel back from a provider is the F143 thread-local:
`gateway_member_caller` populates `_usage_sink.last` (`runner.py:4180-4204`) from
`LocalCouncilModelResult`, and the capture wrapper merges it into `_cap["usage"]`
(`runner.py:2501-2502`). `LocalCouncilModelResult` (`gateway_local.py:63-74`) has
no metadata field. So `num_turns` requires: the async provider result type,
`errorta_council/gateway_local.py` (a Council-invariant boundary — additive
optional field), the sink at `runner.py:4180-4204`, and the reviewer verdict.
Wall time needs nothing new — `duration_ms` is already captured
(`runner.py:2498`).

Then, at the reviewer verdict:

- retrieval was **available** (policy on, root mounted) **and** `num_turns <= 1`
  (the model read nothing) **and** the verdict is `approved: true, findings: []`
  → treat as **unparsed**: retry the turn **once**, with an added instruction to
  open the changed files before deciding.
- if `num_turns` is unavailable (a vendor that does not report it), fall back to
  a wall-time floor `review_min_latency_ms` (policy, **default `0` = off** —
  Δ review, was `3000`). A blanket 3s floor retries *any* fast empty approval:
  a fake provider in tests, a cached CLI response, or a genuinely small diff —
  most approvals — doubling review cost and adding a retry loop to the path this
  batch is de-looping. Opt-in, and only for CLI-class providers.
- if the retry is also ungrounded, **accept the verdict** but record a decision
  (`choice="review_ungrounded"`) and raise a deduped non-blocking Alert
  (mirroring `raise_review_alert`, `attention.py:703`).

Accepting rather than blocking on the second failure is deliberate: a reviewer
that cannot ground its verdict must be *visible*, not a new way to wedge a run.
The revise spiral this batch exists to kill was itself caused by a rejection
nobody could satisfy.

**Acceptance.** An empty approval with `num_turns == 1` is retried once; a second
one is accepted with a `review_ungrounded` decision + one alert. A grounded
approval (retrieval turns > 1) is never retried. With `review_min_latency_ms ==
0` (the default) no verdict is ever retried on latency alone — asserted
explicitly, since that is the false-positive lock; setting it non-zero on a
CLI-class provider retries a sub-floor empty approval once.

## Item 5 — The reviewer sees the latest gate output *(depends on Spec 12)*

**Design.** [Spec 12](SPEC-12-in-loop-acceptance-gate.md) Item 3 adds a shared
`gate_output` prompt segment; the reviewer consumes it in
`_review_pr_prompt_segments` (`runner.py:1713-1768`) after `pr_diff`. The
segment states the head it was produced against, so a reviewer cannot mistake a
stale green for the current tree. The review rules gain one clause: a gate result
that contradicts the diff's claim is a blocking finding — and it comes with a
path, so it satisfies Item 3 naturally.

**Acceptance.** With a red gate on master, the review prompt carries its verbatim
output; with no gate run, the segment is absent and the prompt is byte-identical
to today (golden-locked).

## Item 6 — Screenshot evidence for visual DoDs *(WITHDRAWN 2026-08-06)*

**Withdrawn. The `review_screenshot` knob is deleted, not deferred.**

What actually shipped of this item was the knob alone: `review_screenshot` was
added to `CodingAutonomyPolicy`, round-tripped through
`policy_to_dict`/`policy_from_dict`, asserted by
`tests/coding/test_spec12_18_prep.py`, and **read nowhere**. No capture step ever
existed on the review path. That is worse than an unimplemented item — a status
audit and a green test both reported it as shipped, because the test locked only
the serialization and would have passed either way. A knob an operator can set
that changes nothing is a lie told by the product, so it is removed rather than
left switched off.

**Why it cannot be wired as specified.**

1. **No image can reach a member.** The member seam is
   `MemberCaller = Callable[[dict, str], str]` (`runner.py:86`), and
   `gateway_member_caller` builds `messages=[{"role": "user", "content": prompt}]`
   (`runner.py:7616-7631`). `LocalCouncilModelRequest` /
   `LocalCouncilModelResult` (`gateway_local.py:63-79`) carry no image field.
   Attaching real pixels means changing the Council-invariant boundary type, the
   gateway, the provider, and the seam — for a P2 knob.
2. **The one tool-capable reviewer still cannot open the file.** The `claude_cli`
   retrieval invocation is `--tools "Read,Grep,Glob"` with cwd = the PR worktree
   and **no** `--add-dir` or permission bypass
   (`async_claude_cli.py:296-311`). The probe screenshot is written under the
   ledger directory (`web_probe._screenshot_path` → `<ledger>/web-probe/probe-
   <head>.png`), outside that cwd, so a headless `Read` of it is not permitted.
   Moving the PNG *into* the worktree is not an option either: the worktree is
   staged with `git add -A` (`workspace.py:255`), so the artifact would be
   committed into the delivered product.
3. **A path string alone is not evidence.** Stripped of the pixels, the item
   reduces to pasting an unopenable filename into the prompt.
4. **The substance already reaches the reviewer.** GL01's web probe captures the
   screenshot and folds its verdict — canvas non-black, console-error count,
   whether input changed anything, the failure reason — VERBATIM into the
   `web:probe` run's `stderr_preview` (`web_probe._verdict_to_result`), which
   `gate_state.latest_gate_text` renders into the reviewer prompt's `gate_output`
   segment (`runner.py:3928`). The black-screen case this item was written for
   ("a pure black canvas is invisible to every other signal in this spec") is now
   caught numerically by that probe and reported to the reviewer in words.
   [Spec 28](SPEC-28-autonomy-acceptance.md) already reached the same conclusion
   from the other side: the probe's numeric verdict is the oracle, and the
   screenshot is "a human debugging aid".

**What remains.** `probe_screenshot` stays on the PR record for a human. Real
visual assertions (pixel/DOM diffing) stay where they already were — in Out of
scope / follow-ups — and any future attempt needs a genuine multimodal member
seam, which is a spec of its own, not a knob.

---

**Original design (for the record, superseded by the withdrawal above).**

**Design.** When the project has a runnable `static`/`web` runtime profile (which
[Spec 12](SPEC-12-in-loop-acceptance-gate.md) Item 1 now bootstraps) **and** the
DoD carries a visual bar, capture one headless screenshot of the running merged
head and attach it to the review prompt as an artifact reference.

Reuse the F146 Slice C launch machinery (`_delivery_launch_evidence`,
`runner.py:1853-1900`, `RuntimeProcessManager`) — the process lifecycle, port
allocation, sandboxing, and teardown already exist; this adds a capture step and
an artifact write. Gated behind a policy knob (`review_screenshot`, default
**off** in v1) because it introduces a browser dependency the rest of the engine
does not have.

This is the one item that would have caught the black screen at review time
rather than in a post-mortem: zero console errors, fully initialized state, and a
pure black canvas is invisible to every other signal in this spec.

**Acceptance.** With the knob on and a static runtime profile, a review prompt
for a visual-DoD project references a screenshot artifact of the merged head; with
the knob off, nothing changes. A capture failure degrades to no screenshot, never
to a failed review.

---

## Implementation notes

- **`async_claude_cli.py`** — rename to `repo_read_root` + legacy alias
  (`:180-196`, `:270`); surface `num_turns` on the result (`:370-378`).
- **`gateway_local.py`** — additive optional `num_turns` / `provider_meta` on
  `LocalCouncilModelResult` (`:63-74`). Δ review: previously unlisted.
- **`runner.py`** — reviewer/PM-review dispatch member tagging (`:3392-3410`,
  `:3599`); verdict consumption + `cited` flag + evidence persistence
  (`:3411-3442`); `_usage_sink` carry of `num_turns` (`:4180-4204`, read at
  `:2501-2502`); metadata forwarding (`:4118-4128`); `build_run_turn` signature
  (`:2419-2427`, `:4285-4288`). **No prompt-segment edits** — `gate_output` is
  Spec 12's, `tool_guidance` is Spec 17's.
- **`autonomy.py`** — `reviewer_repo_read`, `review_min_latency_ms` (0) on
  `CodingAutonomyPolicy` (`:63`), round-tripped (`:157/183`) — **landed in the
  shared prep PR**, consumed here. (`review_screenshot` landed there too and has
  since been deleted — Item 6 is withdrawn.)
- **`ledger.py`** — additive PR fields for review grounding evidence
  (`record_pr` `:1017`, `update_pr` `:1047`); absent → falsy, no migration.
  Land in one commit with [Spec 13](SPEC-13-foundation-gate-buildless-web.md)'s
  `unlocks_foundation` — same dict literal.
- **`attention.py`** — reuse `raise_review_alert` (`:703`) for the
  `review_ungrounded` alert; no new raiser needed.

## Edge cases

- **A huge PR worktree**: retrieval is bounded by `--max-turns 16`; the model
  chooses what to read. Same bound the dev has lived with.
- **The reviewer reads the tree and still rubber-stamps**: Item 4 no longer
  fires (retrieval turns > 1), but Item 3 still requires citations for blocking
  findings, and the PR record now carries the grounding evidence for audit. This
  spec makes rubber-stamping *visible*; it cannot make a model care.
- **Retrieval exhausts the budget and falls back** to the plain call: the
  fallback path already logs (`async_claude_cli.py:370-378`) and produces a valid
  verdict; Item 4 sees no retrieval and applies the floor — correct, that verdict
  *is* ungrounded.
- **Non-`claude_cli` reviewer members**: no retrieval, latency fallback governs;
  everything else unchanged.
- **Strict governance dual review** (`runner.py:3599`): the PM PR-review path
  gets the same treatment, otherwise the second review stays blind and the dual
  review is half theater.
- **Truncated diff** (`_REVIEW_DIFF_CAP`, `runner.py:1623`): retrieval makes the
  existing "split this PR" instruction less necessary — the reviewer can open the
  file — but the instruction stays; a reviewer must not approve unseen code on
  the *assumption* it read enough.

## Testing

- **Item 1**: provider builds the retrieval invocation for `repo_read_root` and
  for the legacy alias; neither key → plain argv (byte-identical assertion).
- **Item 2**: a reviewer turn with the policy on carries cwd = the PR worktree
  and the read-only allowlist; a `task_root` failure falls back cleanly; the
  shared member dict is never mutated.
- **Item 3**: uncited blocking finding → `cited: false`, `approved` unchanged,
  PR `changes_requested` and **never** mergeable (the fail-closed lock); cited
  blocking finding → today's behavior; approval persists grounding evidence.
  Suppression of the revise task is tested in Spec 15, not here.
- **Item 4**: `num_turns == 1` + empty approval → exactly one retry; a grounded
  verdict → none; second ungrounded verdict → accepted + one decision + one
  deduped alert; with the default `review_min_latency_ms == 0`, a fast approval
  from the fake provider is **not** retried (the false-positive lock); with it
  set, it is.
- **Item 5**: reviewer prompt golden with and without a gate run.
- **Item 6**: withdrawn. In place of the capture tests,
  `test_spec12_18_prep.py::test_review_screenshot_knob_is_gone_everywhere` asserts
  the knob is offered nowhere (dataclass, policy dict, `errorta_council` source,
  `PM_REFERENCE.md`), and a companion test asserts a persisted
  `review_screenshot` key still loads as a no-op.
- **Integration**: replay a gravity-golf-shaped PR (a rendering module against a
  visual DoD) and assert the reviewer's turn had a mounted worktree and that a
  path-less "no evidence tests were run" finding no longer spawns a `revise:`
  task — the exact spiral trigger.
- Full coding suite + `ruff`.

## Documentation

- `docs/coding/PM_REFERENCE.md`: reviewers now read the PR worktree; findings
  must cite a file; ungrounded verdicts are surfaced as alerts; the new knobs.
- `docs/CLI.md`: `errorta prs` shows review grounding evidence; a new
  `review_ungrounded` alert exists and what it means.

## Out of scope / follow-ups

- Reviewer-driven **reproduction** (running the failing case itself) — the
  analysis's strongest form of independent verification. It needs an execute
  surface for a non-dev role; [Spec 12](SPEC-12-in-loop-acceptance-gate.md) gives
  the reviewer gate *output*, not the ability to run arbitrary commands.
- Retrieval for codex/cursor CLI vendors.
- Real visual assertions (pixel/DOM diffing) beyond attaching a screenshot.
