---
project: Legal Mini
corpus: legal-mini
sensitivity: Public
refresh: manual
description: A small US legal corpus seeded from openly-published government legal documents to exercise the brief-driven collection wedge against an HTML source under a US-Gov-Work license assertion.
tags:
  - legal
  - example
per_doc_max_pages: 40
target_doc_count: 25
target_total_pages: 800
sources:
  - name: generic_html
    config:
      seed_urls:
        - https://www.supremecourt.gov/about/about.aspx
        - https://www.uscourts.gov/about-federal-courts/court-role-and-structure
      license_override: US-Gov-Work
      max_hops: 0
      same_host_only: true
---

# Legal Mini

This brief defines a small, public US legal corpus intended as a smoke-test
for the brief-driven corpus collection pipeline (F008) against the
`generic_html` connector. It pulls a bounded slice of openly-published
federal court pages — chosen because US Government works are not subject
to copyright in the United States and a brief author can assert that
status via `license_override`.

The intent is *not* to be a comprehensive legal knowledge base. It exists
so that downstream features — the collection agent, the corpus manifest,
the judge + grounding loop — have a small, fast, fully-public HTML corpus
to run against without requiring a heavyweight legal data lake.

All documents in this corpus are classified as `Public` and the brief
author asserts a `US-Gov-Work` license via `license_override`. Brief
authors remain responsible for confirming the upstream status of every
seed URL before flipping that field. The refresh cadence is `manual`: the
collection agent runs only when explicitly invoked.
