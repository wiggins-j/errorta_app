# `docs/welcome-corpus-src/` — inputs to the welcome-corpus build script

This directory holds the **hand-authored** Errorta 1-pagers that
`scripts/build-welcome-corpus.sh` bundles into
`dist/welcome-corpus.tar.gz` for the F007 first-run onboarding flow.

## Why this directory exists

The welcome corpus is the first thing a brand-new Errorta user touches.
It must answer:

- What is Errorta?
- How does the judge loop work?
- What is AIAR and why is it the framework?
- How do I add my own files?
- The obvious questions a first-time user has.

Three of those answers (NORTH_STAR, F001, F004) already live in
`docs/` and `docs/specs/`. The script subsets them at build time.

The other three (Built on AIAR, FAQ, How to add your own files) are
**user-facing prose, not implementation detail**, so they live here
under hand-editorial control instead of being generated.

## Rules

1. **Filenames are stable** across welcome-corpus versions. Renaming
   one breaks downstream tooling and confuses returning users.
2. **User-facing tone only.** No implementation detail, no module
   names, no source paths. If a sentence references `errorta_corpus`
   or `routes/judge.py`, rewrite it.
3. **Refresh on each release cut.** Per the F-INFRA-11 spec's Slice
   (e), the maintainer re-reads each file before publishing a new
   welcome-corpus tag and reconciles with current Errorta state.
4. **AIAR is Apache-2.0.** Restate the license in
   `03-built-on-aiar.md` accurately — this is load-bearing for the
   "built on free, open-source AIAR" launch message.

## Layout

| File | Method | Tarball path |
|---|---|---|
| `docs/NORTH_STAR.md` (subsetted) | script subsetter | `docs/00-what-is-errorta.md` |
| `docs/specs/F001-judge-and-grounding-loop.md` (subsetted) | script subsetter | `docs/01-the-judge-loop.md` |
| `docs/specs/F004-corpus-drag-and-drop.md` (subsetted) | script subsetter | `docs/02-corpora-and-rag.md` |
| `03-built-on-aiar.md` | verbatim | `docs/03-built-on-aiar.md` |
| `04-faq.md` | verbatim | `docs/04-faq.md` |
| `05-how-to-add-your-own-files.md` | verbatim | `docs/05-how-to-add-your-own-files.md` |

Refs: `docs/specs/F-INFRA-11-welcome-corpus-tarball.md`.
