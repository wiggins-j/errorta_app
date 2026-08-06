#!/usr/bin/env python3
"""Issue #81, definitive run — is `think: false` needed, or just a bigger budget?

Re-runs the reviewer-verdict compliance measurement from
LOCAL_MODEL_SELECTION_RX9060XT.md §4.1, but crossing the ORIGINAL harness budget
(num_predict=800) against the budget the council actually sends to this model
(8192, from scheduler.REASONING_MAX_OUTPUT_TOKENS, since _is_reasoning_model
matches "qwen3"). Thinking is left ON in the budget arms, because the council never
sends `think` at all.

If compliance goes ~2/6 -> 6/6 on budget alone, the model was never the problem and
`think: false` is treating a symptom of a harness misconfiguration.

Each trial appends a unique nonce to the prompt so Ollama cannot serve a cached
completion — the first pass at this showed eval_count=52 on a repeat of an
identical prompt, which would silently fake a pass.
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://localhost:11434"
BASE_PROMPT = (
    "You are a code reviewer. The diff adds a function `add(a, b)` that returns "
    "`a - b`, while its docstring says it returns the sum.\n\n"
    'Reply with ONLY this JSON object and nothing else: '
    '{"approved": <true|false>, "findings": [<string>, ...]}'
)


def trial(model: str, num_predict: int, think, nonce: int) -> tuple[bool, str, int, int]:
    body = {
        "model": model,
        "messages": [{"role": "user",
                      "content": f"{BASE_PROMPT}\n\n(review id: {nonce})"}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        f"{BASE}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=900).read())
    except Exception as exc:  # noqa: BLE001
        return False, f"ERROR {type(exc).__name__}", 0, 0
    m = d.get("message") or {}
    content = (m.get("content") or "").strip()
    ok = False
    try:
        o = json.loads(content)
        ok = isinstance(o, dict) and "approved" in o and "findings" in o
    except Exception:  # noqa: BLE001
        pass
    return ok, str(d.get("done_reason")), int(d.get("eval_count") or 0), len(content)


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    arms = [
        ("harness budget, thinking ON  ", 800, None),
        ("council budget, thinking ON  ", 8192, None),
        ("harness budget, think:false  ", 800, False),
    ]
    print(f"=== {model} — reviewer-verdict compliance, {trials} trials/arm ===")
    print(f"{'arm':<30} {'schema ok':>10} {'done reasons':>28} {'mean eval':>10}")
    print("-" * 84)
    nonce = 1000
    for label, nb, think in arms:
        oks, reasons, evals = 0, [], []
        for _ in range(trials):
            nonce += 1
            ok, why, ev, _ = trial(model, nb, think, nonce)
            oks += int(ok)
            reasons.append(why)
            evals.append(ev)
        counts = {r: reasons.count(r) for r in sorted(set(reasons))}
        summary = ", ".join(f"{k}:{v}" for k, v in counts.items())
        print(f"{label:<30} {oks:>4}/{trials:<5} {summary:>28} "
              f"{sum(evals)//max(1,len(evals)):>10}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
