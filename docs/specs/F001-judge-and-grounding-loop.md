# F001 — Judge + grounding loop (lead UX)

**Target version:** v0.1
**Status:** implemented
**Owner:** wiggins-j

---

## Problem

Every other local-RAG product (AnythingLLM, LM Studio, PrivateGPT, GPT4All, Khoj) returns an answer and stops. The user has to decide on their own whether the answer is right. They have to manually craft corrections and re-prompt. Quality compounds slowly or not at all.

Errorta's identity is that **every answer comes with a verdict from an LLM-judge**, AND **accepted corrections feed forward into future answers for the same prompt**. This loop is what makes Errorta different. It must be the central experience of the product, not a hidden feature.

## Acceptance criteria

- When the user runs a prompt with the **LLM Judging** toggle on, a structured verdict (`{rating, reason, failure_tags, confidence}`) is displayed next to the answer in under 60 seconds for the recommended model.
- The verdict badge color-codes the rating: green for `good`, yellow for `partial`, red for `bad`.
- If the judge could not produce a usable verdict (timeout, schema failure, unparseable JSON), the UI says so honestly. It does NOT silently fall back to "bad" or hide the error.
- An **"Accept LLM Judge Evaluation"** button is visible only when the verdict has a usable `reason` (and is hidden when `failure_tags` include `judge_failed`, `judge_timeout`, or `judge_unparseable`).
- Clicking Accept records a grounding entry keyed by the original prompt's signature, using the verdict's `reason` as the correction text.
- On a subsequent run of the same prompt with **Reground** on, the recorded correction is prepended to the answerer's input and the result improves measurably.
- The user can also reject the judge's verdict entirely with no permanent effect on grounding.
- The judge's model can be selected independently of the answerer's model (default: same model; override available via Settings).

## UX flow

1. User types a prompt and clicks Run.
2. While the answer is being generated, the verdict badge shows `judge: scoring...` with a spinner.
3. Once both finish:
   - The answer appears at top of the result panel.
   - Underneath, a verdict badge (`judge: good` / `judge: partial` / `judge: bad`) with the judge's `reason` text below it.
   - Below the reason, two buttons in this order: **Mark for Evaluation** | **Accept LLM Judge Evaluation**.
   - To the right, retrieval badges (`RAG: grounded`, `Hybrid`, `Rerank`, `HyDE`, `top-k N`, `Grounding`).
4. On Accept: button disappears, a success message appears in the same row ("Judge verdict accepted and added to grounding.").
5. On a subsequent run of the same prompt with Reground on:
   - The `Reground: applied` badge appears next to RAG badges.
   - The answer shows the model attending to the prior correction.

## Technical approach

Already largely built in AIAR. Errorta's v0.1 work is **polish + extension**, not new code:

- **Already in AIAR (no work needed):**
  - `aiar.eval.judge.judge_answer()` with full Verdict object + JSON parsing + fallback handling
  - `pipeline.answer_prompt(judge=True)` runs the judge in the same pipeline
  - `aiar.grounding.store.record()` persists corrections
  - `grounding_block()` prepends prior corrections when reground is on
  - `/api/evaluation/verdict {call_id, score, correction}` records corrections to the grounding store
  - The "Accept LLM Judge Evaluation" button (added 2026-05-28; we built this)

- **New work for Errorta v0.1:**
  - **Better judge prompts.** The current judge schema is `{rating, reason, failure_tags, confidence}` but some local models (e.g. qwen3.5:9b at 9B params) emit `{verdict: "false", ...}` instead — schema drift. Fix: stricter prompt, schema validation with retry, fallback model tier.
  - **Separate judge model.** Add `EVAL_JUDGE_MODEL` env (and corresponding UI dropdown). When unset, judge uses the active model. When set, judge uses the named model. Pipeline change in `aiar.eval.judge`.
  - **Calmer correction-review UX.** Currently the user clicks Accept and the verdict goes straight to grounding. Add an inline "review correction text" pre-confirmation step: show the proposed correction in an editable text field, let the user trim/edit before submitting.
  - **Verdict metrics dashboard.** A new page that shows: pass rate over time, judge model agreement vs human override rate, most-frequently-corrected prompts. Lives at `/metrics` in the Errorta UI.
  - **Source-jump UI** (overlaps F004): clicking a chunk citation in the answer opens the source document at the right page.

- **Code locations:**
  - Errorta frontend: `src/components/result-panel/`, `src/components/judge-review/`
  - AIAR pipeline: no breaking changes; additive only
  - New: Errorta-side `EVAL_JUDGE_MODEL` env handling in the Tauri sidecar startup

## Dependencies

- [F006](F006-tauri-shell.md) — the Tauri shell must exist to host the UI
- [F003](F003-ollama-management.md) — Ollama must be running for any judge call

## Risks / open questions

- **Judge unreliability with small models.** As we saw on 2026-05-28, qwen3.5:9b sometimes emits wrong-schema JSON or hallucinates errors. Mitigation: ship mistral-small3.1 as the default judge for hardware that can fit it; degrade to qwen for smaller hardware with a UI warning.
- **Grounding store key strategy.** Current: keyed by normalized prompt signature. Risk: minor rephrasing misses the prior correction. Defer fix to F024 (embedding-based grounding keys) post-v1.0; v0.1 documents this limitation.
- **Editing the correction text before accept.** If user edits, do we still call it "the judge's verdict"? UX answer: badge becomes "judge + edit" to be honest about the provenance.
- **Multi-judge consensus.** Out of scope for v0.1 (deferred to F023). v0.1 uses single-judge.
