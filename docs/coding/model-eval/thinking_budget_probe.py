#!/usr/bin/env python3
"""Issue #81 follow-up — is the empty `content` a CHANNEL problem or a BUDGET problem?

The /api/chat matrix showed qwen3.5:9b returning empty `content` whenever thinking
is on, with or without `format: "json"`. Two hypotheses fit that:

  H1 (the issue's): the JSON/format constraint is retargeted onto the thinking
      channel, so the answer never lands in `content`.
  H2 (budget):     thinking consumes the whole `num_predict` budget before the
      model emits its answer, so `content` is empty for want of tokens.

They imply different fixes. H1 -> `think: false`. H2 -> `think: false` OR simply a
larger budget, and `think: false` would be masking a token-accounting bug that will
resurface on any long reasoning turn.

Discriminator: hold thinking ON and sweep `num_predict`. If `content` fills in at a
larger budget, it is H2. If `content` stays empty at every budget while `thinking`
keeps growing, it is H1.

Reports eval_count (tokens actually generated) so budget exhaustion is visible
rather than inferred.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:11434"
PROMPT = (
    "You are a code reviewer. The diff adds a function `add(a, b)` that returns "
    "`a - b`, while its docstring says it returns the sum.\n\n"
    'Reply with ONLY this JSON object and nothing else: '
    '{"approved": <true|false>, "findings": [<string>, ...]}'
)


def chat(model: str, *, num_predict: int, fmt: bool, think: bool | None,
         timeout: int = 600) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    if fmt:
        body["format"] = "json"
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--budgets", default="512,1024,2048,4096,8192")
    ap.add_argument("--trials", type=int, default=2)
    args = ap.parse_args()

    budgets = [int(b) for b in args.budgets.split(",")]
    print(f"=== {args.model} — thinking ON, sweeping num_predict ===")
    print(f"{'budget':>7} {'format':>7} | {'content':>8} {'thinking':>9} {'eval':>6} "
          f"{'done_reason':>12} | verdict")
    print("-" * 92)
    for fmt in (False, True):
        for nb in budgets:
            rows = []
            for _ in range(args.trials):
                raw = chat(args.model, num_predict=nb, fmt=fmt, think=None)
                if "__error__" in raw:
                    rows.append((-1, -1, -1, raw["__error__"][:20]))
                    continue
                msg = raw.get("message") or {}
                c = len((msg.get("content") or "").strip())
                t = len((msg.get("thinking") or "").strip())
                rows.append((c, t, raw.get("eval_count") or 0,
                             str(raw.get("done_reason") or "")))
            c = max(r[0] for r in rows)
            t = max(r[1] for r in rows)
            ev = max(r[2] for r in rows)
            dr = rows[0][3]
            verdict = ("CONTENT PRESENT -> budget was the constraint (H2)"
                       if c > 0 else "content empty")
            print(f"{nb:>7} {str(fmt):>7} | {c:>8} {t:>9} {ev:>6} {dr:>12} | {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
