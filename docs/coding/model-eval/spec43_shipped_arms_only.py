#!/usr/bin/env python3
"""SPEC-43, decision-grade subset: does the SHIPPED config catch planted defects?

WHY THIS EXISTS. The full three-arm matrix is ~6-7 hours, and ~80% of that is the
thinking-on control at ~3 min/call under the production envelope. That control is
not needed for THIS decision, because the question it answers is ABSOLUTE, not
relative: *a reviewer that approves seeded defects is useless regardless of what
any other arm does.* The two `think:false` arms run at ~2 s/call, so the entire
32-item corpus fits in minutes.

This is deliberately NOT a SPEC-43 result — it drops the control, the blind
claim-scoring pipeline, and the per-class statistical machinery. It answers one
question: on the corpus, does the shipped configuration REJECT planted defects and
APPROVE their clean twins? A near-zero rejection rate settles the matter on its own.
A respectable rejection rate does NOT settle it, and the full matrix would then be
worth its wall-clock.

It reuses the harness's own `run_arms`, prompts, envelope and parser, so the
numbers are directly comparable to a full run.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spec43_verdict_usefulness as H  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--arms", default="U,S",
                    help="comma-separated arm names; T is the slow control")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    arms = [a for a in H.ARMS if a.name in wanted]
    if not arms:
        print(f"no arms matched {wanted}; known: {[a.name for a in H.ARMS]}")
        return 2

    items = H.load_corpus()
    by_id = {it.id: it for it in items}
    print(f"corpus: {len(items)} items | arms: {[a.name for a in arms]} "
          f"| trials: {args.trials}", flush=True)

    cfg = H.RunConfig(model=args.model, trials=args.trials, arms=arms,
                      endpoint=args.endpoint)
    records = H.run_arms(items, cfg, progress=lambda s: print(s, flush=True))

    out: dict = {"model": args.model, "trials": args.trials,
                 "arms": [a.name for a in arms],
                 "corpus_integrity": H.corpus_integrity(), "results": {}}

    for arm in arms:
        rs = [r for r in records if r.arm == arm.name]
        seeded = [r for r in rs if by_id[r.item_id].kind == "seeded"]
        clean = [r for r in rs if by_id[r.item_id].kind == "clean"]
        # `approved is False` == the reviewer raised a problem. For a seeded item
        # that is the catch; for a clean twin it is a false rejection.
        caught = [r for r in seeded if r.approved is False]
        passed = [r for r in clean if r.approved is True]
        unparsed = [r for r in rs if not r.parsed]

        per_class: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        per_depth: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for r in seeded:
            it = by_id[r.item_id]
            for bucket, key in ((per_class, it.defect_class), (per_depth, it.depth)):
                bucket[key][1] += 1
                if r.approved is False:
                    bucket[key][0] += 1

        toks = [r.eval_count or 0 for r in rs]
        out["results"][arm.name] = {
            "seeded_rejected": f"{len(caught)}/{len(seeded)}",
            "clean_approved": f"{len(passed)}/{len(clean)}",
            "unparseable": f"{len(unparsed)}/{len(rs)}",
            "mean_eval_tokens": round(sum(toks) / len(toks)) if toks else 0,
            "per_class": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per_class.items())},
            "per_depth": {k: f"{v[0]}/{v[1]}" for k, v in sorted(per_depth.items())},
        }
        print(f"\n{arm.name}: {json.dumps(out['results'][arm.name], indent=2)}",
              flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
        print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
