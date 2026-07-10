#!/usr/bin/env python3
"""F096 B1 — live validation of real AIAR pure-retrieval against example-host.

Exercises the actual code path Council/judge retrieval uses
(``default_pipeline().query`` → ``remote_aiar_retrieve`` → AIAR
``/instances/{instance}/retrieve``) against the configured remote AIAR
(``remote-aiar.json`` / ``ERRORTA_AIAR_REMOTE_URL`` — the maintainer's example-host
server). This is NOT a unit test: it requires a reachable, configured AIAR and
real corpora. Hermetic mapper/routing coverage lives in
``tests/test_f096_b1_remote_retrieve.py``.

Usage:
    PYTHONPATH=python python python/scripts/validate_f096_retrieve_live.py \
        --instance discord-personas --query "what personas are configured"

Exit 0 = retrieval returned ≥1 real chunk through the pipeline; non-zero = the
target was unconfigured/unreachable or returned nothing (printed reason).
"""
from __future__ import annotations

import argparse
import sys

from errorta_query.backend import aiar_retrieval_target
from errorta_query.pipeline import default_pipeline


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="discord-personas")
    ap.add_argument("--query", default="what personas are configured")
    ap.add_argument("--top-k", type=int, default=3)
    args = ap.parse_args()

    target = aiar_retrieval_target()
    if target is None:
        print("FAIL: no remote AIAR configured (remote-aiar.json / "
              "ERRORTA_AIAR_REMOTE_URL). Nothing to validate against.")
        return 2
    print(f"AIAR target: {target[0]} (token: {'yes' if target[1] else 'no'})")

    pipe = default_pipeline()
    print(f"default_pipeline() -> {type(pipe).__name__}")
    if type(pipe).__name__ != "_RemoteRetrievalPipeline":
        print("WARN: pipeline is not remote-retrieval-wrapped; Council retrieval "
              "would not hit the remote AIAR.")

    hits = pipe.query(prompt=args.query, corpus_ids=[args.instance],
                      top_k=args.top_k)
    print(f"retrieved {len(hits)} chunk(s) from '{args.instance}':")
    for h in hits:
        print(f"  score={h.score:.4f} source={h.source!r}")
        print(f"    chunk_id={h.chunk_id} text={h.content[:70]!r}")

    if not hits:
        print("FAIL: 0 chunks — corpus empty for this query, or retrieval broken.")
        return 1
    # provenance must survive the mapping (the whole point of B1)
    if not any(h.source for h in hits):
        print("FAIL: hits carry no source provenance.")
        return 3
    print("OK: real AIAR retrieval through the Council pipeline, with provenance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
