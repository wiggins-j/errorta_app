#!/usr/bin/env python3
"""Issue #81 — the single decisive cell: does the empty-content failure survive at
the budget the COUNCIL actually sends?

`scheduler._is_reasoning_model("qwen3.5:9b")` is True, so a real council turn sends
num_predict=8192 (REASONING_MAX_OUTPUT_TOKENS) with a 300s timeout floor — not the
800 the eval harnesses used, nor the 512 the first matrix run used. Thinking is left
ON because the council never sends `think` at all.

If content is populated at 8192, the failure is BUDGET EXHAUSTION reproduced by a
low `num_predict` in the harness, and the council's own default already avoids it.
If content is still empty at 8192, #81's fix is needed regardless of mechanism.
"""
from __future__ import annotations

import json
import sys
import urllib.request

PROMPT = (
    "You are a code reviewer. The diff adds a function `add(a, b)` that returns "
    "`a - b`, while its docstring says it returns the sum.\n\n"
    'Reply with ONLY this JSON object and nothing else: '
    '{"approved": <true|false>, "findings": [<string>, ...]}'
)


def go(model: str, num_predict: int, think, fmt: bool) -> None:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    if think is not None:
        body["think"] = think
    if fmt:
        body["format"] = "json"
    req = urllib.request.Request(
        "http://localhost:11434/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=900).read())
    except Exception as exc:  # noqa: BLE001
        print(f"num_predict={num_predict:<5} think={str(think):<5} fmt={str(fmt):<5} "
              f"ERROR {type(exc).__name__}: {exc}", flush=True)
        return
    msg = data.get("message") or {}
    content = (msg.get("content") or "").strip()
    thinking = (msg.get("thinking") or "").strip()
    schema_ok = False
    try:
        obj = json.loads(content)
        schema_ok = isinstance(obj, dict) and "approved" in obj and "findings" in obj
    except Exception:  # noqa: BLE001
        pass
    print(f"num_predict={num_predict:<5} think={str(think):<5} fmt={str(fmt):<5} "
          f"content={len(content):<5} thinking={len(thinking):<5} "
          f"eval={data.get('eval_count')} done={data.get('done_reason')} "
          f"schema_ok={schema_ok}", flush=True)
    if content:
        print(f"    -> {content[:150]}", flush=True)


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b"
    # The council's real configuration: reasoning budget, thinking untouched.
    go(model, 8192, None, False)
    # Same budget with the constrained decoding #84 wants to enable.
    go(model, 8192, None, True)
    # The harness budget, for the contrast that explains the original measurement.
    go(model, 800, None, False)
