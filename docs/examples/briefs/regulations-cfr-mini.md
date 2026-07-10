---
project: Regulations CFR Mini
corpus: regulations-cfr-mini
sensitivity: Public
refresh: manual
description: A small US regulations corpus seeded from the Electronic Code of Federal Regulations (eCFR) to exercise the brief-driven collection wedge against a US-Gov-Work HTML source.
tags:
  - regulations
  - cfr
  - example
per_doc_max_pages: 40
target_doc_count: 25
target_total_pages: 800
sources:
  - name: generic_html
    config:
      seed_urls:
        - https://www.ecfr.gov/current/title-14
        - https://www.ecfr.gov/current/title-14/chapter-I
        - https://www.ecfr.gov/current/title-21
        - https://www.ecfr.gov/current/title-21/chapter-I
        - https://www.ecfr.gov/current/title-49
        - https://www.ecfr.gov/current/title-49/subtitle-A
      license_override: US-Gov-Work
      max_hops: 1
      same_host_only: true
---

# Regulations CFR Mini

This brief defines a small, public US federal regulations corpus intended as a
smoke-test for the brief-driven corpus collection pipeline (F008) against the
`generic_html` connector. It pulls a bounded slice of the Electronic Code of
Federal Regulations (eCFR) covering the introductory pages of three
representative titles:

- **Title 14** — Aeronautics and Space (FAA regulations).
- **Title 21** — Food and Drugs (FDA regulations).
- **Title 49** — Transportation (DOT regulations).

The intent is *not* to be a comprehensive regulatory knowledge base. It exists
so that downstream features — the collection agent, the corpus manifest, the
judge + grounding loop — have a small, fast, fully-public HTML corpus to run
against without requiring a full eCFR mirror.

All documents in this corpus are classified as `Public` and the brief author
asserts a `US-Gov-Work` license via `license_override`. Works of the US federal
government are not subject to copyright in the United States (17 U.S.C. § 105),
which is the basis for the override. Brief authors remain responsible for
confirming the upstream status of every seed URL before flipping that field.
The refresh cadence is `manual`: the collection agent runs only when explicitly
invoked.
