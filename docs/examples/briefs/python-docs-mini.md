---
project: Python Docs Mini
corpus: python-docs-mini
sensitivity: Public
refresh: manual
description: A small Python tutorial corpus seeded from the official docs.python.org tutorial to exercise the brief-driven collection wedge against a PSF-2.0 licensed HTML source.
tags:
  - python
  - documentation
  - example
per_doc_max_pages: 40
target_doc_count: 25
target_total_pages: 800
sources:
  - name: generic_html
    config:
      seed_urls:
        - https://docs.python.org/3/tutorial/
        - https://docs.python.org/3/tutorial/introduction.html
        - https://docs.python.org/3/tutorial/controlflow.html
        - https://docs.python.org/3/tutorial/datastructures.html
        - https://docs.python.org/3/tutorial/modules.html
        - https://docs.python.org/3/tutorial/classes.html
      license_override: PSF-2.0
      max_hops: 1
      same_host_only: true
---

# Python Docs Mini

This brief defines a small, public Python tutorial corpus intended as a
smoke-test for the brief-driven corpus collection pipeline (F008) against the
`generic_html` connector. It pulls a bounded slice of the official Python 3
tutorial at `docs.python.org/3/tutorial/`.

The intent is *not* to be a comprehensive Python reference. It exists so that
downstream features — the collection agent, the corpus manifest, the judge +
grounding loop — have a small, fast, fully-public HTML corpus to run against
without requiring a full Python documentation mirror.

All documents in this corpus are classified as `Public` and the brief author
asserts a `PSF-2.0` license via `license_override`. The Python documentation is
distributed under the Python Software Foundation License, which permits
redistribution with attribution. Brief authors remain responsible for
confirming the upstream license of every seed URL before flipping that field.
The refresh cadence is `manual`: the collection agent runs only when explicitly
invoked.
