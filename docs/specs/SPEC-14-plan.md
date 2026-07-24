# Spec 14 — Implementation plan (ground the reviewer)

Spec: [SPEC-14-grounded-reviewer.md](SPEC-14-grounded-reviewer.md).

**Owner:** Engineer A · **Branch:** `feat/spec-14-grounded-reviewer`
**Base:** `chore/spec-12-18-prep` (merged) · **PR into:** `main`
**Land last of Engineer A's three** — Phase 6 consumes Spec 12's `gate_output`
segment, and Phase 3 emits the `cited` flag that Engineer B's Spec 15 reads.

The spec's **Δ review** notes that shape this plan:

- Item 3's original "downgrade the uncited blocking finding" could not stop the
  spiral — the rejection branch keys on `approved`, not on findings. This branch
  now only **produces** the flag; **Spec 15 owns the suppression**.
- `num_turns` has no return channel: `MemberCaller` returns a bare string
  (`runner.py:62`), and `LocalCouncilModelResult` (`gateway_local.py:63-74`) has
  no metadata field. Four files, not two.
- The reviewer `tool_guidance` segment is **Spec 17's**, not this spec's.

## Phase 0 — spec + plan (no code)

Branch off the merged prep PR; commit the spec + this plan.

## Phase 1 — generalize the retrieval mechanism (provider-side, no behavior change)

1. `async_claude_cli.py:180-196` — `_dev_repo_read_root` → `_repo_read_root`,
   accepting `metadata["repo_read_root"]` **and** the legacy
   `dev_repo_read_root` alias (a mixed-version sidecar/gateway pair must keep
   working). Rename `_DEV_REPO_READ_TOOLS` → `_REPO_READ_TOOLS` and
   `_DEV_REPO_READ_MAX_TURNS` → `_REPO_READ_MAX_TURNS`; **values unchanged**
   (`"Read,Grep,Glob"`, `16`). Keep the "if `--tools` semantics ever change, this
   branch must not ship" comment verbatim — it is the safety rationale.
2. `runner.py:4118-4128` — forward whichever key the member carries.

**Tests.** `test_dev_repo_read_retrieval.py`: the retrieval invocation is built
for `repo_read_root` and for the legacy alias; neither key → the plain argv,
asserted byte-identical.

## Phase 2 — reviewer turns mount the PR worktree

At the reviewer dispatch (`runner.py:3392-3410`) and the strict-mode PM PR-review
dispatch (`runner.py:3599-3612`), when `policy.reviewer_repo_read` is on, tag a
**per-turn shallow copy** of the member (never mutate shared config — mirror
`runner.py:3257-3261`) with `repo_read_root =
workspace.task_root(pr["task_id"], branch=pr["branch"])` — the PR's own tree, the
one the verdict is about. Thread `reviewer_repo_read` through `build_run_turn`
(`runner.py:2419-2427`, `:4285-4288`) alongside `dev_repo_read`.

Best-effort: any resolution failure falls back to the unchanged member.
Retrieval must never convert a turn that would have succeeded into a failure —
the provider's empty-result fallback (`async_claude_cli.py:365-380`) is the
second line of defence.

**No prompt-segment edit in this phase.**

**Tests.** With the policy on, the reviewer turn's invocation has cwd = the PR
worktree and tools = `Read,Grep,Glob`; with it off, byte-identical to today; a
`task_root` failure degrades cleanly; the shared member dict is never mutated
(assert identity/contents of the original).

## Phase 3 — the `cited` flag on findings

At the verdict boundary (`runner.py:3411-3442`): a `blocking` finding whose
`path` is empty or names a file not in `workspace.changed_paths(branch)` ∪ master
is marked `cited: false`. **Severity untouched. `approved` untouched. The PR
still goes `changes_requested`.**

The prompt rule that makes this satisfiable ("cite a file; `file:line` in the
body is better") rides in Spec 17's reviewer `tool_guidance` segment — coordinate
the wording, do not add a segment here.

**Tests.** Uncited blocking finding → `cited: false`, `approved` unchanged, PR
`changes_requested` and **never** mergeable (the fail-closed lock); a finding
citing a changed file → `cited: true` and today's behavior; a non-blocking
finding is unaffected. Suppression of the revise task is Spec 15's test, not
this one's.

## Phase 4 — `num_turns` return channel

The seam, in order:

1. `async_claude_cli.py:370-378` — surface `obj["num_turns"]` on the provider
   result (it is read today only for a log line).
2. `gateway_local.py:63-74` — additive optional `num_turns` (or a
   `provider_meta` dict) on `LocalCouncilModelResult`. A Council-invariant
   boundary: additive and optional only.
3. `runner.py:4180-4204` — `gateway_member_caller` writes it into `_usage_sink.last`;
   the capture wrapper already merges that into `_cap["usage"]` (`:2501-2502`).
   `duration_ms` is already captured (`:2498`) — the latency fallback needs no
   new plumbing.

**Tests.** A fake provider reporting `num_turns` surfaces it on the parsed turn;
one that omits it yields `None` and nothing raises.

## Phase 5 — ungrounded-verdict retry

At the reviewer verdict: retrieval available **and** `num_turns <= 1` **and**
`approved: true, findings: []` → treat as unparsed, retry **once** with an added
instruction to open the changed files. If `num_turns` is unavailable, fall back to
`review_min_latency_ms` — **default `0` (off)**; a blanket 3s floor would retry
most approvals (fake providers, cached CLI responses, small diffs), doubling
review cost and adding a loop to the path this batch is de-looping.

A second ungrounded verdict is **accepted**, with a `review_ungrounded` decision
and one deduped alert (reuse `raise_review_alert`, `attention.py:703`). Blocking
here would be a new way to wedge a run — the exact failure this batch exists to
kill.

Persist the grounding evidence (retrieval ran / turns / wall time) on the PR
record (`update_pr`, `ledger.py:1047`) — **same commit as
[Spec 13](SPEC-13-plan.md)'s `unlocks_foundation`**, since both add keys to
`record_pr`'s dict literal (`ledger.py:1017`).

**Tests.** `num_turns == 1` + empty approval → exactly one retry; grounded
verdict → none; second ungrounded → accepted + one decision + one deduped alert;
**with the default `review_min_latency_ms == 0`, a fast fake-provider approval is
not retried** (the false-positive lock); set non-zero → retried once.

## Phase 6 — gate output in the review prompt *(after Spec 12 merges)*

Spec 12 Item 3 owns the `gate_output` segment in
`_review_pr_prompt_segments` (`runner.py:1713-1775`); this phase adds only the
**review-rule clause**: a gate result contradicting the diff's claim is a blocking
finding — and it comes with a path, so it satisfies Phase 3 naturally.

**Tests.** Reviewer prompt golden with and without a gate run.

## Phase 7 — screenshot evidence *(P2, knob default off)*

Behind `review_screenshot` (default `False`): with a runnable `static`/`web`
profile and a visual DoD, capture one headless screenshot of the merged head and
attach an artifact reference to the review prompt. Reuse F146 Slice C's launch
machinery (`runner.py:1853-1900`, `RuntimeProcessManager`) — lifecycle, ports,
sandboxing and teardown already exist; this adds capture + an artifact write.

Ship this phase only if Phases 1–6 are green and reviewed; it is the one part
that introduces a browser dependency. Splitting it into its own PR is acceptable.

**Tests.** Knob off → nothing changes; knob on with a stubbed capture → the
artifact reference appears; a capture exception → no screenshot, never a failed
review.

## Phase 8 — integration + docs

- Replay a gravity-golf-shaped PR (a rendering module against a visual DoD) and
  assert the reviewer turn had a mounted worktree and that a path-less "no
  evidence tests were run" finding is flagged `cited: false`.
- `docs/coding/PM_REFERENCE.md` — reviewers read the PR worktree; findings must
  cite a file; ungrounded verdicts surface as alerts; the new knobs.
- `docs/CLI.md` — `errorta prs` shows review grounding evidence; the
  `review_ungrounded` alert.

## Definition of done

Full coding suite + `ruff` green. Fail-closed lock asserted (an uncited finding
never makes a PR mergeable). No `tool_guidance` segment touched. No revise-task
suppression in this branch — that is Spec 15's.
