# Local Model Selection for the Errorta Coding Council

**Target hardware:** Radeon RX 9060 XT 16 GB — Linux + Ollama with the bundled ROCm backend
**Date:** 2026-08-06
**Audience:** Errorta engineers / PM
**Status:** Measured. All numbers below come from runs on the actual box, not estimates.
**Scope:** local-model selection and Ollama integration for the coding council.

---

## Executive summary

**Recommendation: run `qwen3.5:9b` for every coding-council role**, configured with
`think: false`, `format: "json"`, and `num_ctx: 16384`.

It leads on code correctness by a wide margin (92.3% vs 78.7% / 74.9%), ties the
code specialists on structured-output compliance, and comfortably fits the GPU
with room for a second resident model. It is slower in raw tok/s than
`qwen2.5-coder:7b`, and that trade is worth taking: the 7b produces fully-correct
code less than half as often, and in a `PM → DEV → REVIEWER → TESTER` loop a wrong
implementation costs a full extra cycle.

Three defects were found along the way. The first is more valuable than the model
choice itself:

1. **F001's "qwen3.5:9b is unreliable at the judge schema" is a thinking-mode
   integration bug, not a model weakness.** Setting `think: false` fixes it
   completely. The proposed mitigation of shipping `mistral-small3.1` (15 GB) as
   the default judge is likely unnecessary.
2. **Two model-selector bugs** make automatic route selection unusable on an
   all-local pool.
3. **Constrained decoding is off.** Enabling `format` takes clean, directly
   parseable JSON from 0/6 to 6/6.

---

## 1. Hardware baseline

Verified on the box, not assumed:

| | |
|---|---|
| GPU | Radeon RX 9060 XT, Navi 44, **gfx1200** |
| VRAM | **15.9 GiB** usable |
| Board power cap | **182 W** (hard driver ceiling) |
| CPU | Intel i5-6600K, **4 cores** |
| System RAM | **15 GiB** |
| Ollama | 0.24.0, bundled ROCm backend (`libggml-hip.so`) |
| Model store | dedicated NVMe volume |

Two environment notes that affect any local-model guidance for this host:

- **No ROCm userland is installed.** There is no `rocm-smi` and no `/opt/rocm`;
  Ollama ships its own ROCm backend. Guidance that says to verify with
  `watch -n 1 rocm-smi` does not work here. Use `ollama ps` plus
  `/sys/class/drm/card1/device/mem_info_vram_used`.
- **hipBLASLt has no tuned kernels for gfx1200.** Ollama logs
  `rocblaslt error: Cannot read "TensileLibrary_lazy_gfx1200.dat"` on every load.
  Non-fatal — it falls back to a slower path — but prompt-processing throughput
  is likely leaving performance on the table. Worth revisiting after an
  Ollama/ROCm update.

**System RAM is the binding constraint, not VRAM.** With 15 GiB of RAM and a
4-core 2015-era CPU, any model that spills off the GPU is not merely slow, it is
unusable. `gemma3:27b` (17 GB) and `mistral-small3.1` (15 GB) are both in this
category on this host.

---

## 2. Methodology

Three experiments, all run on the test host against live Ollama.

### 2.1 Sustained soak — stability, thermals, power

Two concurrent agents cycling role-shaped prompts (PM planning, DEV TDD
implementation, REVIEWER structured verdict, TESTER verification) for 7 minutes
per model at `num_ctx=16384`, sampling GPU power, temperature, utilisation and
VRAM every 2 seconds. 21 minutes of continuous load total.

### 2.2 Structured-output compliance

6 trials per model per shape, over the two shapes the council depends on: the
REVIEWER `approve`/`request_changes` verdict and the PM task decomposition with
`difficulty_tier`. Scored on schema validity and on whether the output was clean
JSON or needed prose/fence extraction. Run both raw (current council behaviour)
and with Ollama `format: "json"`.

### 2.3 Code correctness — execution-based

8 Python tasks, **61 hidden pytest tests the model never sees**, 3 trials each at
temperature 0.2. Generated code is imported and executed; scoring is test-pass
rate plus an all-or-nothing full-task pass rate. Generated code runs in a temp
directory under a subprocess with `RLIMIT_CPU`, `RLIMIT_AS` and a 15 s timeout.

Tasks were drawn from work AIAR and the council actually do rather than generic
puzzles: `chunk_text` (RAG chunking with overlap), `rrf_fuse` (reciprocal rank
fusion), `topo_sort_tasks` (task DAG with cycle detection), `parse_frontmatter`,
`token_bucket` (stateful, injectable clock), `parse_duration`, `merge_ranges`
(half-open intervals, no-mutation contract), `truncate_middle`.

Suites emphasise what separates working code from plausible-looking code: error
paths, off-by-one boundaries, determinism contracts, and state discipline.

**Test-suite validation.** Reference implementations were written for all 8 tasks
and run against the suites first. This caught a contradictory spec in
`truncate_middle` that would have unfairly failed every model. After the fix, all
8 suites pass 61/61 against reference. Results below are therefore measuring the
models, not the harness.

---

## 3. Results

### 3.1 Code correctness (the deciding axis)

| Model | Tests passed | Full-task pass | Perfect & stable (3/3 trials) |
|---|---|---|---|
| **`qwen3.5:9b`** | **169/183 (92.3%)** | **20/24 (83.3%)** | **6 of 8 tasks** |
| `qwen2.5-coder:7b` | 144/183 (78.7%) | 11/24 (45.8%) | 3 of 8 |
| `qwen2.5-coder:14b` | 137/183 (74.9%) | 9/24 (37.5%) | 3 of 8 |

Per-task detail (passed per trial):

| Task | `qwen3.5:9b` | `coder:7b` | `coder:14b` |
|---|---|---|---|
| `chunk_text` (8) | **8, 8, 8** | 6, 6, 6 | 7, 7, 7 |
| `rrf_fuse` (8) | **8, 8, 8** | **8, 8, 8** | **8, 8, 8** |
| `topo_sort_tasks` (8) | **8, 8, 8** | 7, 6, 6 | 2, 0, 4 |
| `token_bucket` (9) | **9, 9, 9** | 6, 6, 9 | **9, 9, 9** |
| `merge_ranges` (9) | **9, 9, 9** | **9, 9, 9** | 8, 8, 8 |
| `truncate_middle` (5) | **5, 5, 5** | **5, 5, 5** | **5, 5, 5** |
| `parse_duration` (5) | 5, 1, 5 | 3, 3, 1 | 4, 4, 4 |
| `parse_frontmatter` (9) | 8, 1, 8 | 3, 1, 9 | 7, 1, 0 |

Findings:

- **`qwen3.5:9b` wins decisively** — 14 points on tests, nearly double the
  full-task rate.
- **The 14b is worse than the 7b.** The larger, slower, more expensive code
  specialist scored lower on both metrics, and collapsed on `topo_sort_tasks`
  (`2, 0, 4`) and `parse_frontmatter` (`7, 1, 0`).
- **Neither coder model ever got `chunk_text` fully right**, across all trials.
  `qwen3.5:9b` was 3/3 clean. This matters — chunking is AIAR's ingest path.
- `qwen3.5:9b`'s two weak tasks were re-run to rule out a harness artifact; code
  extracted cleanly every time, so the variance is genuine model noise on those
  two tasks only.

The 7b-vs-14b gap (78.7 vs 74.9) is within noise for an 8-task benchmark. The
`qwen3.5:9b` lead over both is not.

### 3.2 Structured-output compliance

6 trials per cell. "Clean" = directly parseable, no prose or fences to strip.

| Model | Reviewer valid | PM valid | Clean raw | Clean with `format` |
|---|---|---|---|---|
| `qwen2.5-coder:7b` | 6/6 | 6/6 | 0/6 | **6/6** |
| `qwen2.5-coder:14b` | 6/6 | 6/6 | 0/6 | **6/6** |
| `qwen3.5:9b` (`think:false`) | 6/6 | — | 5/6 | **6/6** |
| `qwen3.5:9b` (thinking on) | 2/6 | 0/6 | — | 0/6 in `response` |

**All three models are fully schema-compliant when configured correctly.**
Compliance is not a differentiator; configuration is.

### 3.3 Soak — stability, thermals, power

7 minutes per model, 2 concurrent agents, `num_ctx=16384`:

| | `qwen3.5:9b` | `coder:7b` | `coder:14b` |
|---|---|---|---|
| Requests completed | 27 | **49** | 25 |
| Generation | 38.5 tok/s | **60.6 tok/s** | 30.3 tok/s |
| Prompt processing | — | **4329 tok/s** | 2221 tok/s |
| Avg / p95 latency | — | **17.8 / 22.1 s** | 34.5 / 42.0 s |
| Placement | 100% GPU | 100% GPU | 100% GPU |
| VRAM resident | 9.2 GB | 6.3 GB | 13.0 GB |
| Power avg / peak | 115 / 153 W | 164 / 172 W | 170 / **175 W** |
| Temp peak | 54 °C | 57 °C | **59 °C** |
| Errors | 0 | 0 | 0 |

- **All three run 100% on GPU at 16K context.** Ollama confirms full offload
  (`33/33`, `29/29`, `49/49` layers). Nothing touched the CPU.
- **Zero faults** — no GPU resets, no machine-check exceptions, no thermal or
  power errors in the kernel log across the entire 21-minute run.
- **Throughput was flat** (`coder:7b` held 60.2–61.4 tok/s over 49 requests),
  indicating no throttling and no memory pressure.
- **Two models co-reside.** `ollama ps` showed `coder:7b` (6.3 GB) and
  `qwen3.5:9b` (8.7 GB at 4K) both loaded at 100% GPU simultaneously. At 16K each
  it is ~15.5 GiB against 15.9 available — too tight to rely on.

### 3.4 Concurrency

Ollama currently runs with **`Parallel:1`**, so concurrent agents **serialize**.
Two simultaneous identical requests:

| | tok/s | wall clock |
|---|---|---|
| Request B | 38.5 | 10.9 s |
| Request A | 38.6 | **21.5 s** |

Each gets full speed, but the second waits the full duration of the first. No
corruption, no failure — just a latency doubling that worsens with every added
agent.

KV cache is allocated **per parallel slot**. Measured cost is ~0.35 GiB per 1K
context for a 9B model, against a ~9.7 GiB KV budget:

| Config | KV total | Fits |
|---|---|---|
| 2 agents × 8K | 5.6 GiB | yes |
| 2 agents × 16K | 11.2 GiB | **no** — spills to CPU |
| 2 agents × 16K, `q8_0` KV | ~5.6 GiB | yes |
| 4 agents × 8K, `q8_0` KV | ~5.6 GiB | yes |

Flash attention is already enabled, which is the prerequisite for KV cache
quantization.

### 3.5 Power / PSU

Relevant if this GPU has a history of PSU trouble under gaming load.

- Sustained inference peaked at **175 W against the 182 W cap** — it never
  reached the ceiling the driver enforces.
- **Concurrency does not raise peak power.** Two concurrent agents peaked at
  151 W, identical to single-stream, because the work serialized. Even with true
  parallelism the 182 W cap is a hard ceiling.
- Inference is **gentler on a PSU than gaming**: no microsecond transients (the
  trace was nearly flat at ~148 W), and the CPU stays near-idle instead of
  spiking alongside the GPU.
- The genuinely different risk is **duration** — near-constant load for hours
  versus bursty gaming. A degraded supply can survive spikes but fail under
  sustained heat soak. No faults occurred in 21 minutes; the failure signature to
  watch for is a hard reboot under load.

---

## 4. Defects found

### 4.1 Thinking mode breaks structured output — the F001 fix

**Severity: high.** `qwen3.5:9b` is a thinking model. With thinking enabled and
`format: "json"`, the JSON constraint applies to the **thinking channel** and
`response` comes back empty:

```json
{ "response": "",
  "thinking": "{\"action\": \"generate_response\",
                \"content\": {\"verdict\": \"approve\", \"findings\": []}}" }
```

Any caller reading `response` gets an empty string. Measured impact on the
REVIEWER verdict:

| Config | Valid | In `response` |
|---|---|---|
| raw, thinking on | 2/6 | 2 |
| `format:json`, thinking on | 6/6 | **0** (all in `thinking`) |
| raw, `think:false` | 5/6 | 5 |
| **`format:json`, `think:false`** | **6/6** | **6** |

[`docs/specs/F001-judge-and-grounding-loop.md`](../specs/F001-judge-and-grounding-loop.md)
records qwen3.5:9b "emitting wrong-schema JSON" and proposes shipping
`mistral-small3.1` as the default judge for hardware that can fit it. On this
hardware that is a 15 GB model that cannot co-reside with anything.

**The evidence says the model is fine and the integration is wrong.** This is
consistent with `errorta_council/gateway_local.py:25` already carrying a prefix
workaround for "thinking-only Ollama responses" — that workaround treats the
symptom.

**Recommended:** set `think: false` on every structured turn, then re-evaluate
whether the `mistral-small3.1` judge fallback is needed at all.

---

#### 4.1a Follow-up validation — the mechanism above is wrong; the cause is the token budget

The §4.1 measurement used `/api/generate`. The council does **not** use that route:
`gateway_local._ollama_dispatch` posts to `/api/chat`. Re-running on `/api/chat`
against the same model on the same box changes the conclusion.

**The stated mechanism does not reproduce.** On `/api/chat`, thinking-on returns an
empty `content` with **or without** `format`, and the thinking channel holds *prose*,
not the `{"action": "generate_response", …}` JSON §4.1 captured. So "the JSON
constraint is applied to the thinking channel" is not what is happening here —
`format` is not the trigger.

**What is actually happening: the generation is truncated.** Repro scripts:
[`model-eval/thinking_format_matrix.py`](model-eval/thinking_format_matrix.py),
[`model-eval/decisive_budget_test.py`](model-eval/decisive_budget_test.py),
[`model-eval/budget_vs_think.py`](model-eval/budget_vs_think.py).

| `num_predict` | thinking | `format` | `content` | `done_reason` | schema |
|---|---|---|---|---|---|
| 512 | on | off | **empty** | `length` | — |
| 512 | on | on | **empty** | `length` | — |
| 800 (harness) | on | off | 1/6 valid | `length` ×5, `stop` ×1 | 1/6 |
| **8192 (council)** | **on** | off | **274 chars** | **`stop`** | **valid** |
| 8192 | on | on | 274 chars | `stop` | valid |

`qwen3.5:9b` emits ~8.3k characters (~1,950 tokens) of thinking before it answers.
Any budget under ~2,000 truncates it mid-thought — `done_reason: length`, not a
malformed model. **The eval harnesses used `num_predict: 800`**
([`schema_test.py:105`](model-eval/schema_test.py), [`retest_qwen35.py:61`](model-eval/retest_qwen35.py))
and `1600` ([`correctness.py:543`](model-eval/correctness.py)) — roughly a tenth of
what the council sends.

**The council was never affected on this path.** `scheduler._is_reasoning_model`
matches `"qwen3"`, so a real turn already gets
`REASONING_MAX_OUTPUT_TOKENS = 8192` plus a 300 s timeout floor. The comment at
`scheduler.py:1727` names this exact failure mode: a low budget "makes them emit a
thinking-burn with no answer."

**Consequences.**

* **F001's `mistral-small3.1` recommendation is unfounded** *on this evidence*. The
  judge-schema failure it cites is reproduced by a harness misconfiguration, not by
  the model. The 15 GB co-residency cost buys nothing here. (It does not follow that
  F001's *observed* judge problems were all this — they were seen through a
  different harness. It follows that §4.1 is not the evidence for them.)
* **`think: false` is not required, and would mask this.** It "works" because
  suppressing thinking frees the budget for the answer — the same reason a larger
  budget works. Shipping it would leave the truncation bug in place for any long
  turn.
* **The `THINKING_TRACE_MARKER` workaround (`gateway_local.py:25`) is the real
  smell.** When `content` is empty it substitutes `MARKER + thinking`, so the
  council receives the reasoning trace *presented as an answer* — which is exactly
  how a truncated thinking model would show up as F001's "wrong-schema JSON".

**Recommended instead:** keep thinking on; make the truncation loud rather than
silent. Specifically — fail a turn whose `done_reason == "length"` on a structured
route instead of passing the marker-prefixed thinking text downstream as if it were
a verdict, and raise `num_predict` for any route whose observed thinking regularly
approaches the cap.

**Caveats.** One model (`qwen3.5:9b`), one prompt shape, on a box concurrently
serving the FastAPI LLM service, so timings are contended. The 8192 + `format` cell
returned `eval_count=52` on an identical repeated prompt — almost certainly KV-cache
reuse — so that row is weaker than the `format`-off row (`eval_count=1958`); the
6-trial re-run uses per-trial nonces to defeat caching.

### 4.2 All local routes are hardcoded to `mid`

**Severity: high for all-local deployments.**
`errorta_council/coding/model_tier.py:42` returns `MID` for anything starting
`local.` before inspecting the model name:

```python
if rid.startswith(("local.", "fake.")):
    return MID
```

Verified by executing the real functions:

```
local.qwen3.5:9b        tier=mid    local.qwen2.5-coder:7b   tier=mid
local.qwen2.5-coder:14b tier=mid    local.gemma3:27b         tier=mid
```

Two consequences, both confirmed by running the selector:

- A task with `difficulty_tier: "strong"` returns **`NoCapableModel`** on an
  all-local pool. Strong tasks are unservable.
- `next_escalation_assignment` requires *strictly* greater rank
  (`minimum_rank_exclusive`), so with every local route tied at `mid` the
  **escalation ladder can never fire**.

**Recommended:** derive the tier from the model name for local routes as well, or
document `metadata.model_tier` / `model-catalog-overrides.json` as required
configuration for local-only teams.

### 4.3 Substring collision in size hints selects an unloadable model

**Severity: high.** `errorta_council/coding/model_catalog.py:60` tests bare
substrings:

```python
if any(token in low for token in ("nano","mini","haiku","flash","lite","3b","7b")):
    return 0, 0
```

`"27b"` contains `"7b"`. So `local.gemma3:27b` is tagged smallest-and-fastest.
With all four local models pooled, the selector chose:

```
difficulty=light  -> local.gemma3:27b
difficulty=mid    -> local.gemma3:27b
difficulty=strong -> NoCapableModel
```

**It selects the one model that cannot fit in 15.9 GiB VRAM.** On this host that
means CPU spill on a 4-core i5 with 15 GiB RAM.

Also mis-ranks `13b`, `17b`, `23b`, `37b`, `47b`, and similar. `70b` is
unaffected by luck (no `7b` substring).

**Recommended fix** — anchor the parameter-count match:

```python
import re
_SIZE = re.compile(r"[:\-_](\d+(?:\.\d+)?)b\b")

def _param_billions(route_id: str) -> float | None:
    m = _SIZE.search(route_id.lower())
    return float(m.group(1)) if m else None
```

then band on the numeric value rather than substring membership.

### 4.4 Constrained decoding is never used

**Severity: medium.** The council never sends Ollama's `format` parameter;
structured verdicts rely purely on prompt adherence. Raw prompting produced
*valid* JSON but wrapped in prose or fences **every single time** (0/6 clean),
meaning every structured turn depends on tolerant regex extraction. With
`format`, output is 6/6 clean and directly parseable.

**Recommended:** enable `format` (or a JSON schema) for structured turns on
non-thinking routes. Do **not** enable it for thinking models without also
setting `think: false` — see 4.1.

---

## 5. Recommended configuration

### Model assignment

Run **`qwen3.5:9b` for all four roles** (PM, DEV, REVIEWER, TESTER). It leads on
correctness on every task type tested, and the throughput deficit is outweighed
by needing far less rework.

Do not adopt `qwen2.5-coder:14b`. It is the slowest, largest, and least correct of
the three.

`qwen2.5-coder:7b` remains a reasonable choice where latency dominates and output
is independently verified — it is 1.6× faster and its 6.3 GB footprint leaves room
for a second resident model.

### Ollama service

```bash
OLLAMA_NUM_PARALLEL=2        # else concurrent agents serialize
OLLAMA_KEEP_ALIVE=-1         # avoid cold reloads between turns
OLLAMA_KV_CACHE_TYPE=q8_0    # required for 2 slots at 16K
```

### Per-request options

```json
{ "think": false,
  "format": "json",
  "options": { "num_ctx": 16384 } }
```

`think: false` on every structured turn — this is the 4.1 fix.
`format: "json"` on structured turns only, never with thinking enabled.

---

## 6. Limitations

- **8 tasks, 61 tests, 3 trials.** Enough to separate models that differ
  substantially; not enough to split a 2–3 point gap. The 7b-vs-14b difference is
  within noise. The `qwen3.5:9b` lead is not.
- **Python only.** No coverage of TypeScript, Rust, or the Tauri/`src-tauri` side
  of Errorta.
- **Single-turn generation.** Does not measure multi-turn repair, tool use, or
  long-horizon agentic behaviour — all of which the council exercises heavily.
- **No end-to-end council run.** Roles were simulated with role-shaped prompts,
  not driven through the real scheduler and ledger.
- **Concurrency measured at `Parallel:1`.** True `NUM_PARALLEL=2` aggregate
  throughput was not measured; it requires an Ollama restart.

The highest-value follow-up is an end-to-end run of the real council loop against
a live repo, scored on merge-gate pass rate rather than isolated function
correctness.

---

## 7. Reproducing

Harnesses used:

| Script | Purpose |
|---|---|
| `soak.py <model> <num_ctx> <minutes>` | Sustained 2-agent load, power/thermal/throughput sampling |
| `schema_test.py <trials>` | Reviewer + PM schema compliance, raw vs `format:json` |
| `correctness.py <trials>` | 8 tasks / 61 hidden pytest tests, execution-scored |
| `reference.py` | Reference solutions used to validate the hidden suites |

Models pulled for this evaluation: `qwen2.5-coder:7b`, `qwen2.5-coder:14b`.
Already present on the test host: `qwen3.5:9b`, `gemma3:27b`, `mistral-small3.1`,
`nomic-embed-text`.
