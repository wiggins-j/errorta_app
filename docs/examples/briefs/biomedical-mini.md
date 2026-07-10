---
project: Biomedical Mini
corpus: biomedical-mini
sensitivity: Public
refresh: manual
description: A small biomedical corpus seeded from open-access PubMed Central pages to exercise the brief-driven collection wedge against an HTML source.
tags:
  - biomedical
  - example
per_doc_max_pages: 40
target_doc_count: 25
target_total_pages: 800
sources:
  - name: generic_html
    config:
      seed_urls:
        - https://www.ncbi.nlm.nih.gov/pmc/about/intro/
        - https://www.ncbi.nlm.nih.gov/pmc/about/openftlist/
      license_override: CC-BY
      max_hops: 0
      same_host_only: true
---

# Biomedical Mini

This brief defines a small, public biomedical corpus intended as a smoke-test
for the brief-driven corpus collection pipeline (F008) against the
`generic_html` connector. It pulls a bounded slice of PubMed Central's
open-access information pages — chosen because the upstream open-access
subset is openly licensed and a brief author can assert that license via
`license_override`.

The intent is *not* to be a comprehensive biomedical knowledge base. It
exists so that downstream features — the collection agent, the corpus
manifest, the judge + grounding loop — have a small, fast, fully-public
HTML corpus to run against without requiring a heavyweight PubMed mirror.

All documents in this corpus are classified as `Public` and the brief
author asserts a CC-BY license via `license_override`, which is the
intended consent moment for redistribution. Brief authors are responsible
for confirming the upstream license before flipping that field. The
refresh cadence is `manual`: the collection agent runs only when
explicitly invoked.
