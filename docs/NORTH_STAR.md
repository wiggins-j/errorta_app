# Errorta — North Star

This document is the source of truth for product identity. Every roadmap item, spec, and design decision should trace back to something here. If a feature doesn't serve this north star, it doesn't ship.

---

## Product identity (one sentence)

> **Errorta is the AI workbench where you choose what stays local, what goes remote, and when a stronger model is worth the cost.**

That sentence is the long-term homepage hero once
the user-selected model gateway (F030) ships. Until that
capability is present, launch copy must not imply remote or hybrid model
routing exists. The pre-F030 public hero remains anchored on the judge
loop: Errorta is the AI that admits when it is wrong and remembers your
corrections.

The judge wedge still matters. The broader promise is that the user, not
the app, decides whether that loop runs entirely locally, uses a remote
model for support, or escalates hard prompts to a stronger remote model.

---

## The verb

What does a user actually **do** with Errorta?

> They run private, custom RAGs over their own data, choose where each
> model role runs, and systematically improve answer quality with a
> visible verdict and correction path.

The verb is: **query my own data, choose the execution boundary, and
trust the result.**

Trust is the load-bearing word. Every other local-RAG product gets you to "answer" but stops there. Errorta gets you to "answer + verdict + correction path."

---

## Who it's for

**Tech users.** Developers, AI hobbyists, researchers, privacy-conscious power users who already know what a local LLM is and want a polished, opinionated stack instead of stitching one together themselves.

We are **not** building for:
- Non-technical users who've never heard of an LLM (yet — that's a future tier)
- Enterprises with IT procurement processes (not in v1.0 scope)
- Cloud-only developers who just want a hosted API with no local control

If a feature decision boils down to "would this make the experience better for a non-technical user but worse for a developer?" — we pick the developer.

---

## The three wedges

These are the things Errorta does that no comparable local-AI product does. They ship in order. Each is a separate marketing story.

### 1. Judge + grounding loop (v0.1 — the lead story)

Every answer is scored by an LLM-judge in the same pipeline. The judge produces a structured verdict: `{rating, reason, failure_tags, confidence}`. When you (or the judge) catch a mistake, you click **Accept LLM Judge Evaluation** — the correction is recorded into the grounding store and feeds forward into future answers for the same prompt.

**Tweet-pitch:** *"Local RAG that learns from its own mistakes."*

This is what's already in AIAR today. Errorta's job for v0.1 is to make the experience polished — better judge prompts, the option to use a different model for judging, a metrics dashboard, source-jump from chunk citations, and a calm, opinionated UX around the accept/reject correction flow.

### 2. Brief-driven corpus collection (v0.3 — the workflow story)

Write a markdown brief that describes the corpus you want (sources, schema, dedup rules, sensitivity gates, refresh policy) and an AI agent goes and builds it. Multi-source. Compliance-gated. The aerospace corpus inside AIAR was built this way — 489 docs across 7 sources from a single `.md` file.

**Tweet-pitch:** *"Don't have a corpus? Write a brief, get a corpus."*

This is the AnythingLLM-killer for anyone who doesn't already have a folder of PDFs sitting on disk. Currently lives in AIAR's developer tooling — productizing it for v0.3 means a brief library, a UI to run/refresh briefs, and a status dashboard.

### 3. Service API for sibling apps (v1.x — the infrastructure story)

`/services/prompt` is built for **other applications on your machine** to use Errorta as their local LLM backend. They push `{prompt, instance (corpus), model, system, ...}`, get a grounded, judged response back.

**Tweet-pitch:** *"The AI workbench any of your apps can call."*

Already exists in AIAR. Errorta's job is to make sibling-app integration first-class: an SDK library, examples, an integrations gallery.

---

## Brand promise

F030-era homepage promise:

> Errorta is the AI workbench that doesn't pretend to be right. Every answer comes with the model's own verdict on whether it's any good — and when it's not, you can record the correction, and Errorta remembers. You choose local, remote, or hybrid; Errorta makes that choice visible.

Pre-F030 public launch copy should keep the same trust wedge without
claiming remote routing:

> Errorta is the AI workbench that doesn't pretend to be right. Every answer comes with the model's own verdict on whether it's any good — and when it's not, you can record the correction, and Errorta remembers. Local by default. No hidden cloud.

---

## Non-goals

The things we're explicitly NOT building. Saying no to these is part of the product.

- **Hidden cloud.** No remote model calls, sync, accounts, telemetry, or
  background uploads without explicit user opt-in. Local-only remains the
  default mode and an absolute kill switch.
- **Multi-tenant / multi-user.** One install, one user. Teams are a future v2+ concern.
- **Beating LM Studio at smooth onboarding.** They polish for breadth; we go deep on the judge loop. We don't compete on first-run UX for non-technical users.
- **Being everything to everyone.** The three wedges define what we are. Features that don't serve them get punted to "maybe v2."
- **Mobile (iOS / Android).** Desktop only. Mobile is a separate product if it ever happens.
- **A chat-only product.** The judge + correction loop is the central UX, not the chat surface. Chat ships in v0.2 as a *delivery vehicle* for the loop, not as the headline.

---

## Built on AIAR

Errorta is the polished product layer on top of [AIAR](https://github.com/wiggins-j/aiar), our free, open-source local-AI framework. AIAR ships the substrate: hybrid retrieval (BM25 + vector + RRF), cross-encoder reranking, HyDE query rewriting, the LLM-as-judge pipeline, the grounding store, the service API. Errorta wraps it in a Tauri desktop app, adds hardware scanning, drag-and-drop file ingestion, an opinionated correction-review UX, and the polish that makes it ready for users who don't want to clone a Python repo.

This split matters. When Errorta ships, **AIAR ships alongside it**, both free. The framework stays open and developer-first. The product is the curated, polished experience. Anyone who wants a different UX can build their own on top of AIAR — we're shipping the one we want to use.

Every page of Errorta's website will advertise this. Not because we have to, but because it's true.

---

## Distribution and economic model

- **Free download** from a GitHub Pages site (custom domain TBD)
- **Source code:** private until the v1.0 launch. After launch, license TBD (Apache-2.0 to match AIAR is the path of least friction; MIT also on the table).
- **Revenue model:** none for v1.0. Money is not the goal. If someone wants to acquire it later, that conversation can happen then.

---

## What success looks like

In rough order:

1. **v0.1 ships** and at least 10 tech users who aren't me try it and tell me what's wrong.
2. **v1.0 launches publicly** on a GitHub Pages site, with signed Mac binaries. Front page of HN at least once.
3. **Reputation:** "Errorta is the one where the AI judges its own answers" becomes a sentence people say about local AI.
4. **AIAR's PyPI install count** reflects Errorta's distribution — the framework rides the product's reach.
5. **Optional:** someone offers to buy it, or to fund continued work. Not required for the project to be a success.

Success is **not** measured by users or revenue. It's measured by whether the three wedges become recognized as a real contribution to the local-AI ecosystem.
