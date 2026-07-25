# Why the Council Didn't Converge

## A citation-grounded analysis of a role-decomposed multi-agent coding pipeline without an execution loop

*Research method: 5-angle parallel literature sweep → 21 primary sources fetched → 103 falsifiable claims extracted → top 25 adversarially verified (3 independent refutation votes each; 25/25 confirmed, 0 refuted). Claims marked **[inference]** are this report's reasoning beyond the cited evidence.*

---

## 1. Executive summary

**The central hypothesis is supported. Confidence: HIGH on the mechanism, MODERATE on exact attribution.**

The hypothesis — that this pipeline underperforms a single strong agent with a write→run→observe→fix loop because the decomposition removed executable feedback while adding coordination overhead — is supported by convergent evidence from four independent literatures, and every one of the run's four pathologies is separately named, measured, and mitigated in primary sources:

1. **Failure taxonomies recognize these exact modes.** MAST ("Why Do Multi-Agent LLM Systems Fail?", NeurIPS 2025) categorizes multi-agent failures into system design issues, inter-agent misalignment, and **task verification failures** — the last covering "no or incomplete verification" and "incorrect verification," which is precisely a reviewer approving/rejecting work nothing ever executed [1]. A second taxonomy of failed SWE-bench runs names **Non-Progressive Iteration**, **Blind Strategy Switching**, and **Validation Retreat** (agents sabotaging their own tests) — the last matching your ground-truth "test harness that sabotaged itself" almost verbatim [2].

2. **Execution feedback is the load-bearing ingredient, not roles.** SWE-agent showed a *single* LM with the ability to run tests reached then-SOTA 12.5% on SWE-bench versus ~3.8% for non-interactive approaches [3]. AlphaCodium more than doubled the *same model's* accuracy (pass@5 19%→44%) purely by wrapping it in a run-against-tests loop [4]. A 178-submission survey of the SWE-bench leaderboards found executing tests against the real repo is the dominant validation strategy among all leading systems [5]. Your pipeline had six agents and zero executions.

3. **Ungrounded critics behave exactly as your reviewer did.** Execution-free LLM code review exhibits systematic over-rejection — false-rejection rates of 26–92% across models and benchmarks, and *rising* when the reviewer is prompted to explain and propose fixes [6]. On CodeJudgeBench, most non-thinking judges score below 60% against a 50% coin-flip baseline, with up to 14pp swings from answer ordering alone [7]. Your reviewer's 70% rejection rate on diff excerpts is not an outlier; it is the measured behavior of this configuration.

4. **Iteration without a reliable oracle diverges.** Intrinsic self-correction without external feedback degrades performance (Huang et al., ICLR 2024) [8]; self-debugging steered by unreliable self-generated tests *reduces* pass rates [9]; failed agent runs consume 3.5× more steps than successes, dominated by "cognitive deadlocks" [2]. Your 14-deep revise chains sit far outside the empirical success envelope (median successful runs converge in 11–16 rounds; the long tail is almost entirely failures).

5. **Head-to-head, the direction favors single-agent.** A controlled evaluation of seven agent frameworks found single-agent systems beat multi-agent on all three SE tasks tested, with role-decomposed frameworks repeatedly emitting malformed patches *that no collaborating agent caught* [10]. The leaderboard survey found no measurable accuracy advantage for multi-agent architectures, and documents teams (nFactorial, Warp) that empirically *abandoned* multi-agent designs after observing coordination losses [5]. MAST's motivating observation is that MAS gains over simpler baselines are "often minimal" [1].

**Why not full confidence:** no published study benchmarks this *exact* configuration (diff-only reviewer, never-dispatched tester, write-only devs) against a single execution-loop agent — support is triangulated, not a single controlled head-to-head. The decomposition of blame between "missing execution loop" and "coordination overhead" cannot be quantified from the run data; the literature suggests missing execution is primary **[inference]**, because single agents *with* execution succeed where multi-agent systems *with* execution merely break even [5], while nothing without execution converges on tasks like this. Also, honest counterpoint: ungrounded self-critique is not literally worthless — Self-Refine showed ~20pp gains on single-artifact tasks [11] — but that result is for tight single-context loops on short artifacts, not PR-mediated multi-agent revision at repo scale.

**One-line verdict:** the run instantiated a known anti-pattern — *N narrower copies of one model, minus the feedback loop, plus coordination surface* — and the literature both predicts the observed dynamics (blind rejection, revise churn, test sabotage, tool confabulation) and prescribes the fix: ground the loop in execution, gate iteration on measured progress, and collapse roles that have no capability to discharge their responsibility.

---

## 2. Failure taxonomy mapped to citations

| Observed in the run | Recognized name | Source | Evidence grade |
|---|---|---|---|
| 0 TESTER turns; nothing ever executed the artifact | **Task verification failure**: FM-3.2 "no or incomplete verification" | MAST [1] | Peer-reviewed (NeurIPS 2025 D&B); 1600+ annotated traces, κ=0.88 |
| Reviewer rejects on unverifiable grounds ("no evidence tests were run") from diff excerpts | FM-3.3 "incorrect verification"; **overcorrection / false rejection** in execution-free review | MAST [1]; Springer ASE study [6]; CodeJudgeBench [7] | Peer-reviewed, benchmarked (FNR 26.2–91.9%) |
| 14-deep revise chains; 67/96 PRs rejected; endless re-do | **Non-Progressive Iteration**; **cognitive deadlock** (~65% of root causes); self-correction divergence | Issue-solving taxonomy [2]; Huang et al. [8]; stability-threshold analysis [12] | Preprint (large-N trace analysis) + peer-reviewed (ICLR 2024) |
| Test harness that sabotaged itself | **Validation Retreat** — agents weakening/modifying the test rather than the code | Issue-solving taxonomy [2] | Preprint; documented verbatim category (C2.3) |
| ~12 backlog tasks demanding execution assigned to write-only agents; hallucinated "run tests" tool calls | **Capability–role / agent-computer-interface mismatch** (closest named concept: ACI design as first-class determinant; MAST "system design issues") | SWE-agent [3]; MAST [1] | Peer-reviewed; but note: no source names "capability–role mismatch" verbatim — this mapping is defensible, not a citation **[inference]** |
| 96 PRs, 53 superseded, 30% merge rate; foundation-gate thrash | **Inter-agent misalignment**; coordination overhead: information loss at handoffs, inter-agent hallucination, undetected error propagation | MAST [1]; agent-frameworks evaluation [10]; leaderboard survey [5]; Cognition [13] | Mixed: peer-reviewed taxonomy + benchmarked correction-rate data + practitioner report |
| Whole-run non-convergence despite frontier model in every seat | **MAS minimal-gain phenomenon**; compute-controlled analyses attribute reported MAS wins to extra compute, not architecture | MAST [1]; Tran & Kiela-style compute-controlled studies (via [1] verification notes) | Peer-reviewed for "often minimal"; compute-attribution corroboration secondary |

Two taxonomy caveats, honestly stated: (a) MAST catalogs coordination *failure modes*, not coordination *overhead cost* — the efficiency claim rests on [10] and [5]; (b) the issue-solving taxonomy [2] describes a *single* agent's iteration failures; applying "Non-Progressive Iteration" to a DEV↔REVIEWER pair is an interpretive extension **[inference]**, though a natural one since the pair jointly implements one revise loop.

---

## 3. Per-pathology findings and recommendations

### Pathology 1 — No execution loop (severity: critical; fix first)

**Recognition.** Universal. The entire agentic-SE literature since 2024 treats executable feedback as the core mechanism. SWE-agent's framing: the agent-computer interface — including the ability to *execute tests and programs* — is what enables automated software engineering; ACI design choices alone moved success by 10.7pp in ablations [3]. The issue-solving taxonomy shows that in modern agentic tools ~half of all *failures* now occur inside the Iteration & Validation stage [2] — i.e., the run/observe/fix loop is where the hard work happens; your pipeline deleted that stage entirely, which removes not an optimization but the decisive phase **[inference]**.

**Mechanisms in leading systems.**
- *SWE-agent / OpenHands*: sandboxed shell + test execution in the agent loop [3].
- *AlphaCodium*: generate tests first, then iterate run→fix against them; "test anchors" forbid any fix that breaks a previously-passing test — an explicit anti-oscillation mechanism [4].
- *SWE-bench leaderboard systems (178 submissions)*: regression tests plus issue-reproduction scripts as the dominant patch-validation strategy [5].
- *ConvCodeWorld*: weaker models *with* execution feedback outperform single-turn frontier models *without* it — feedback can matter more than raw model strength [14].

**Evidence strength.** The strongest of the four: peer-reviewed, benchmarked, replicated across models (AlphaCodium's gains reproduce on GPT-3.5 and DeepSeek, so it's the flow, not the model [4]).

**Recommendations (prioritized).**
1. **Give at least one role a real executor, and route every merge through it.** For this deliverable (buildless web), that's a headless browser: load `index.html`, capture console errors, screenshot, assert non-black canvas after N frames, run any registered tests. All three ground-truth bugs (harness sabotage, init race, trivial levels — the last partially) surface under exactly this probe.
2. **Never gate the TESTER on a registry nothing populates.** Ship a default probe suite (page loads, zero console errors, smoke interaction) that runs unconditionally, and make "populate the test registry" an early mandatory task with an executable acceptance check. A verification role that can silently never fire is MAST FM-3.2 by construction [1].
3. **Adopt test anchors** [4]: once a probe passes on master, every subsequent PR must keep it passing — this converts the reviewer's unverifiable "did you break something?" into a mechanical check and prevents revise-loop oscillation.
4. **Attach execution evidence to PRs.** The PR object should carry probe output (logs, screenshots, pass/fail) as first-class data; then review is grounded (see Pathology 2) and "no evidence tests were run" becomes impossible rather than fatal.

### Pathology 2 — Ungrounded review (severity: critical; fix with #1)

**Recognition.** Well-established and quantified. Execution-free LLM code review systematically over-rejects correct code: false-rejection rates 26.2–91.9% across 5 models and 3 benchmarks; making the reviewer produce explanations and proposed corrections — which review-role prompts almost always do — *raises* misjudgment (GPT-4o FNR 26.2%→73.2% on HumanEval) [6]. CodeJudgeBench (deliberately execution-free judging, and with *more* context than your reviewer had): most non-thinking judges below 60% vs a 50% random baseline; best judge 82%; 14pp position bias; accuracy varies by the *style* of the code's author [7]. A survey of LLM-as-judge in SE reports a static judge below 42% agreement with ground truth that jumped to ~72% when given an execution tool [15]. Your reviewer — diff excerpts only, no repo, no execution — sits at the worst-instrumented end of every spectrum measured; CodeJudgeBench is a *conservative* bound on its unreliability **[inference from configuration comparison]**.

The reviewer wasn't malfunctioning, either: "no evidence tests were run" was *true*. It was the correct verdict inside an unsatisfiable gate — the pipeline demanded evidence no role could produce. That is a system-design failure (MAST category i), not a critic failure **[inference]**.

**Mechanisms in leading systems.**
- *Execution-backed verdicts*: the Fix-guided Verification Filter runs original and revised code against tests before honoring a rejection — average FNR drops 54.8%→16.3% (HumanEval), 69.0%→28.9% (MBPP) [6].
- *Reference-grounded critique*: an LLM critic given the gold test patch predicts executability at F1 91.6% and build status for 84.8% of SWE-bench instances *without running code* — but beats reference-free critics by 38.9–72.5%, i.e., grounding in a test-linked artifact is what closes the gap [16].
- *Repo-grounded rubrics*: letting the critic explore the repository before judging beats all ungrounded verifier baselines (+3.5 Best@16 minimum); removing repo access costs 4.0 points and makes criteria ambiguous. Even then, 46% of its rejections-of-passing-patches were low-utility artifacts — grounded review approximates but does not replace execution [17].
- *Format levers*: pairwise comparison beats pointwise scoring (+8pp human agreement in one study [18]); full-context inputs beat stripped excerpts [7].
- *Cautionary result*: naively bolting interaction onto a judge can make it *worse* (53.1% vs 66.1% agreement when errors compound across an agentic judging pipeline [18]) — grounding must be simple and verifiable, not just "agentic."

**Evidence strength.** Strong and multiply corroborated for the failure; strong for execution-backed mitigation; moderate for repo-access-only mitigation. Caveat: most numbers are function-level benchmarks with 2024–25 models; transfer to repo-scale diff review is extrapolation.

**Recommendations (prioritized).**
1. **Split the review into machine-checked and judgment lanes.** Anything executable (does it load, do probes pass, do anchors hold) is decided by the executor from Pathology 1, never by the LLM. The LLM reviews only what a diff can actually evidence: design, clarity, spec conformance.
2. **Forbid rejection on unverifiable grounds.** If the rejection reason references runtime behavior or test execution, the pipeline must either attach execution evidence or route to the executor — not bounce to a DEV who also can't verify it.
3. **Give the reviewer the repo, not an excerpt** [7][17] — cheapest single upgrade if full execution-grounding is deferred.
4. **Treat reviewer verdicts as advisory, cap their veto.** With FNRs this high, an ungrounded reviewer with absolute veto is a randomized rejection machine; two rejections on the same task should escalate to the PM with the disagreement surfaced, not spawn revise #3.

### Pathology 3 — Capability–role mismatch (severity: high)

**Recognition.** Partially named. No paper says "capability–role mismatch" verbatim — this is the weakest naming match of the four **[explicitly flagged]**. The nearest established results: SWE-agent establishes that what an agent is *granted* (actions + observations) is a first-class determinant of success, and that LM agents need purpose-built interfaces [3]; MAST's "system design issues" category covers role/spec misdesign [1]; and the Cognition essay's principle that subagents must be scoped to what their context and tools support [13]. Your ledger's 8 `tool_failed` + 7 `write_failed` events — a DEV hallucinating a "run tests" tool — is the confabulation signature of an agent assigned a task its interface cannot discharge; no paper directly studies hallucinated calls to ungranted tools, so that causal link is inference from the ACI results **[inference]**.

**Mechanisms in leading systems.**
- *ACI design as an explicit engineering discipline* — grant the tools the task requires, shape observations to the agent [3].
- *Scoped delegation* — Claude Code's subagent pattern (cited by Cognition as the safe design): subagents get narrow, answerable questions precisely because they lack the main agent's context [13].
- *Tools over roles* — the agent-frameworks evaluation's headline recommendation: adding specialized tools grounded in the environment beats adding dedicated agents/roles [10].

**Evidence strength.** Moderate. The ACI ablations are peer-reviewed and quantitative [3]; the specific "assign-execution-to-non-executors → churn" loop is documented only in your ledger, though it is over-determined by Pathologies 1+2.

**Recommendations (prioritized).**
1. **Lint the backlog at dispatch.** Static check: if a task's description demands execution ("run", "diagnose failures", "verify the gate") and the assignee's tool grant has no executor, the dispatcher must block it, reroute to an execution-capable role, or rewrite the task. ~12 of your backlog tasks were unsatisfiable at creation; this check is nearly free.
2. **Make the PM capability-aware.** The PM's planning prompt should include the actual tool grants per role, so it stops generating tasks no role can perform.
3. **Treat hallucinated tool calls as a telemetry alarm, not noise.** An agent inventing a "run tests" tool is telling you what interface the task needs [3] **[inference]** — page the run governor, don't just log `tool_failed`.
4. **Collapse roles that can't discharge their duty.** A TESTER that structurally never dispatches and a REVIEWER that can't verify are not doing their jobs with degraded quality — they're doing *different* jobs (nothing, and noise-injection, respectively). Either grant capability or delete the role [10][13].

### Pathology 4 — Uncapped revise churn (severity: high)

**Recognition.** Well-established under several names. Huang et al. (ICLR 2024): intrinsic self-correction without external feedback degrades accuracy across all tested models/benchmarks (GPT-4 GSM8K 95.5→89.0 after two rounds), earlier positive results depended on oracle stop-labels, and when a model changes its answer it's more often correct→incorrect than the reverse [8]. Self-debugging steered by self-generated tests reduces pass rates because the verification signal itself is biased (suites only 44.5–59.2% accurate; false negatives dominate) — the loop is steered by a faulty judge [9]. Your DEV↔REVIEWER loop is exactly this structure: a faulty judge steering blind revisions **[inference — the papers study single-agent loops; the transfer is structural]**. The issue-solving taxonomy quantifies the dynamics: ~65% of failures are "cognitive deadlocks" (persisting with a failing strategy), failed runs take 3.5× more steps, 80% of successes land within 19–25 rounds (medians 11–16) while the failure tail stretches past 54 rounds — the authors explicitly recommend halting unproductive attempts [2]. A recent stability analysis gives the clean condition: iteration helps only while the error-correction rate exceeds the error-introduction rate scaled by current accuracy; below that threshold, more rounds are net harmful, and prompt-level "verify-first" gating flipped a −6.2pp degradation to +0.2pp [12]. Honest counterpoints: Self-Refine's ~20pp gains show ungrounded refinement can work on short single-context artifacts [11], and Reflexion hit 91% on HumanEval — but Reflexion's reflection is conditioned on *actual test feedback*, which is precisely the signal your loop lacked [19].

**Mechanisms in leading systems.**
- *Grounded stop conditions*: iterate against executable tests; stop on pass (Reflexion [19], AlphaCodium [4]).
- *Anchors against oscillation*: never accept a revision that regresses a previously-passing check [4].
- *Budgets/circuit breakers*: halt unproductive attempts rather than iterating indefinitely; the success-envelope statistics justify a hard cap [2].
- *Gated self-correction*: verify-first prompting; treat revision as a measured control decision, not an always-on reflex [12].
- *Strategic oversight over more iteration*: an Expert reviewer grounded in the failure taxonomy, intervening at decision points, solved 22.2% of previously-unsolvable instances — replanning beats revising when deadlocked [2].

**Evidence strength.** Strong for degradation-without-oracle (peer-reviewed, replicated) and for diminishing returns (large trace analysis, preprint). Fixed-depth caps specifically are supported *by extension* — the taxonomy authors recommend dynamic progress assessment; exact optimal stopping is open.

**Recommendations (prioritized).**
1. **Hard-cap revise chains at ~3, replan at the cap.** Given medians of 11–16 *total* rounds for successful full tasks [2], a 14-deep chain on one task was ~14 draws from a judge with a 26–92% false-rejection rate **[inference]**. On cap: escalate to the PM with the full rejection history for task redefinition, decomposition, or abandonment — echoing the Expert-Executor result [2].
2. **Add progress detection.** Flag deadlock when rejection reasons repeat or successive diffs are near-identical (Non-Progressive Iteration [2]); flag oscillation when a revision reverts an earlier one. Either condition breaks the loop immediately, before the cap.
3. **Require each revision to state what changed and which rejection point it addresses**, and have the gate check the claim against the diff — a cheap verify-first analog [12].
4. **Count superseded work as a run-level health metric.** 53 superseded PRs and a 30% merge rate is a convergence alarm the governor should act on (freeze fan-out, force integration) long before a 3h20m timeout **[inference — operational extension]**.

---

## 4. Multi-agent vs single-agent, and parallelism (RQ5–6)

**RQ5 — the crossover.** The direct evidence favors your operator's observation:

- Controlled comparison of seven frameworks: single-agent won all three SE tasks; in program repair, role-decomposed frameworks scored 3–10% on SWE-bench Lite vs 53–54% for tool-equipped single agents, with malformed patches propagating *undetected through the collaboration* — and a 45.1% correction rate for the multi-agent orchestrator vs 15.4% for SWE-Agent, which is your rework churn, measured [10].
- Across 178 SWE-bench leaderboard submissions: no measurable accuracy edge for multi-agent; top single-agent 73.2% vs best multi-agent workflow 75.2% (a wash); and named teams consolidated multi-agent designs into single agents after observing context loss at handoffs [5].
- MAST: MAS gains over simpler baselines "often minimal," with compute-controlled follow-ups attributing much of the reported MAS advantage to extra inference compute rather than architecture [1].

Where multi-agent *does* help, in the evidence surveyed: breadth (multi-agent scored higher on requirements-coverage completeness even while losing on executability [10]) and *structured oversight* rather than symmetric collaboration (the Expert-Executor +22.2% result [2]). The tentative crossover rule **[inference]**: decomposition pays when subtasks are independently verifiable and context-light, or when the second agent adds a *different signal* (grounded verification, strategic review) — not when it adds another ungrounded opinion in the same loop.

**RQ6 — parallel agents on a shared codebase.** Thinnest evidence area; flagged rather than papered over. The surviving verified claims contain no quantified merge-churn/contract-drift study. What exists: Cognition's practitioner argument that parallel subagents embed conflicting implicit decisions that downstream agents cannot reconcile, with single-threaded, full-context execution as the fix and inter-agent dialogue *not* a reliable conflict-resolution mechanism [13]; plus the handoff-information-loss findings in [10] and [5]. Your 53 superseded PRs and the concurrency clamp-to-1 are consistent with these accounts, but the field lacks the benchmark that would let me cite churn rates. Treat any parallelism design as engineering judgment, not literature-backed: the safest evidence-adjacent pattern is serialized integration against a continuously-verified master with strictly partitioned file ownership **[inference]**.

---

## 5. Annotated bibliography (load-bearing sources)

1. **Cemri et al., "Why Do Multi-Agent LLM Systems Fail?" (MAST)** — arXiv:2503.13657; NeurIPS 2025 D&B spotlight. The canonical MAS failure taxonomy: 14 modes, 3 categories, 1600+ annotated traces over 7 frameworks (MetaGPT, ChatDev, AG2…), κ=0.88. Anchors the taxonomy mapping and the "gains often minimal" finding; concludes failures need structural redesign, not prompt patches. *Peer-reviewed; the field's reference point.*
2. **"An Empirical Study of Failures in LLM-based Automated Issue Solving"** — arXiv:2509.13941 (Sept 2025 preprint; peer-review unconfirmed). 150 failed SWE-bench-Verified instances → 3-phase/9-category/25-subcategory taxonomy naming Non-Progressive Iteration, Blind Strategy Switching, Validation Retreat, Context Amnesia; cognitive-deadlock root-cause split (~65/25/10); 3.5× step inflation in failures; iteration success envelopes; Expert-Executor +22.2%. *The single most case-relevant source; weight tempered by preprint status.*
3. **Yang et al., "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering"** — arXiv:2405.15793; NeurIPS 2024. Single LM + execution-capable ACI → 12.5% SWE-bench pass@1 vs ~3.8% non-interactive; ACI ablations worth 10.7pp. *Grounds both the execution-centrality and the interface-mismatch arguments.*
4. **Ridnik et al., "Code Generation with AlphaCodium"** — arXiv:2401.08500. Same-model test-driven flow doubles CodeContests pass@5 (19→44%); test anchors as anti-regression; gains replicate across GPT-3.5/DeepSeek; ~4 orders of magnitude more compute-efficient than AlphaCode sampling. *Vendor-authored (Qodo), small validation set, open-sourced and reproduced.*
5. **"An Empirical Study of SWE-bench Leaderboard Submissions"** (178 systems) — arXiv:2506.17208. Execution-based validation dominant across leading systems; no single-vs-multi accuracy advantage; documented practitioner retreats from multi-agent (nFactorial, Warp); independently corroborates MAST categories on 200 MAS executions. *Best available field-wide architectural evidence.*
6. **"Are LLMs Reliable Code Reviewers? Systematic Overcorrection…"** — arXiv:2603.00539; Automated Software Engineering (Springer). FNR 26.2–91.9%; elaborate review prompts worsen it; execution-backed Fix-guided Verification Filter cuts FNR ~3–4×. *The closest experimental replica of your reviewer's failure; function-level scope.*
7. **"CodeJudgeBench"** — arXiv:2507.10535. Execution-free judging benchmark, 26 judges, 5,352 pairs: near-chance non-thinking judges, 82% ceiling, 14pp position bias, author-style bias, pairwise>pointwise, full-context>stripped. *Conservative upper bound on a diff-excerpt reviewer.*
8. **Huang et al., "LLMs Cannot Self-Correct Reasoning Yet"** — arXiv:2310.01798; ICLR 2024. Intrinsic self-correction degrades performance; prior gains depended on oracle stopping; debate ≤ self-consistency at equal compute. *The foundational negative result for ungrounded revision loops.*
9. **"Revisiting Self-Debugging…"** — arXiv:2501.12793; ACL 2025. Self-generated-test-guided debugging lowers pass rates (suites only 44.5–59.2% accurate); wrong-but-detailed feedback worse than bare pass/fail; execution-trace grounding restores modest gains. *Directly shows a faulty judge makes iteration harmful.*
10. **"Comprehensive Empirical Evaluation of Agent Frameworks on Code-centric SE Tasks"** — arXiv:2511.00872 (preprint). Single-agent beats multi-agent on all three tasks; undetected error propagation through collaboration; 45.1% vs 15.4% correction rates; "add tools, not agents." *The most direct RQ5 head-to-head; recent, peer-review unconfirmed.*
11. Supporting: **Self-Refine** (arXiv:2303.17651, NeurIPS 2023 — the honest counterpoint); **Reflexion** (arXiv:2303.11366, NeurIPS 2023 — refinement works *when fed real test signal*); **self-correction stability threshold** (arXiv:2604.22273 — ECR/EIR condition, verify-first gating); **reference-aware critics** (arXiv:2501.16655, AWS); **repo-grounded rubric verification** (arXiv:2601.04171); **LLM-judge-in-SE survey** (arXiv:2510.24367 — 42%→72% with an execution tool); **ConvCodeWorld** (arXiv:2502.19852); **web-dev judge reliability** (arXiv:2510.18560); **Cognition, "Don't Build Multi-Agents"** (blog — practitioner evidence, weighted accordingly).

---

## 6. Open questions and weak evidence

Stated plainly rather than smoothed over:

1. **No direct replica experiment.** Nothing benchmarks *this exact* pipeline shape against a single execution-loop agent on the same task. The hypothesis is confirmed by triangulation (taxonomy + ablation + judge-reliability + head-to-head-adjacent studies), which is strong but not a controlled A/B. The causal attribution for *this run* remains inference.
2. **Blame decomposition is unquantified.** How much of the 70% rejection churn was the ungrounded reviewer versus genuinely bad blind-written code is unknowable from the ledger — the DEVs also had no feedback, so both error sources were maximized simultaneously. The literature cannot separate them for you; an ablated re-run (same pipeline + execution gate only) could.
3. **RQ6 is thin.** No quantified study of merge churn / contract drift for parallel LLM agents on a shared repo surfaced. Practitioner reports (Cognition) and handoff-loss findings are directional only. This is a genuine gap in the field, not just in this search.
4. **Benchmark-to-repo transfer.** The judge false-rejection and self-debugging numbers come from function-level benchmarks (HumanEval/MBPP/CodeContests) on 2024–25 models. Whether 2026 frontier models reviewing realistic repo-scale diffs show the same rates — and how much repo-access-without-execution recovers versus full execution grounding — is measured only indirectly [17].
5. **Optimal stopping is open.** Fixed-depth caps are supported by extension from the success-envelope data; the taxonomy authors prefer dynamic progress assessment [2], and whether cognitive deadlock is reliably detectable *online* from trajectory features is an open research question.
6. **Where multi-agent genuinely wins.** Compute-controlled conditions under which role decomposition beats a single execution-loop agent (very long horizons? genuinely independent subtasks? diverse-signal verification panels?) are not yet mapped. The breadth-vs-executability tradeoff in [10] and the Expert-Executor result in [2] are the best current hints.
7. **Peer-review status.** Two load-bearing sources (arXiv:2509.13941, arXiv:2511.00872) are preprints. Conclusions resting *solely* on them (the deadlock percentages, the all-three-tasks head-to-head margin) should be held a notch looser than those anchored by MAST, SWE-agent, Huang et al., and the ACL/Springer studies.
