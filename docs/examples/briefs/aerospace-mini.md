---
project: Aerospace Mini
corpus: aerospace-mini
sensitivity: Public
refresh: manual
description: A small aerospace corpus seeded from arXiv to exercise the brief-driven collection wedge end-to-end.
tags:
  - aerospace
  - example
per_doc_max_pages: 40
target_doc_count: 25
target_total_pages: 800
sources:
  - name: arxiv
    config:
      categories:
        - cs.RO
        - astro-ph.IM
      date_from: '2024-01-01'
---

# Aerospace Mini

This brief defines a small, public aerospace corpus intended as a smoke-test for
the brief-driven corpus collection pipeline (F008). It pulls a bounded slice of
recent arXiv preprints from two relevant categories: `cs.RO` (robotics) and
`astro-ph.IM` (astrophysics — instrumentation and methods).

The intent is *not* to be a comprehensive aerospace knowledge base. It exists so
that downstream features — the collection agent, the corpus manifest, the judge
+ grounding loop — have a small, fast, fully-public corpus to run against
without requiring the full 489-document aerospace RAG.

All documents in this corpus are sourced from openly-licensed preprints and
classified as `Public`. The refresh cadence is `manual`: the collection agent
runs only when explicitly invoked.
