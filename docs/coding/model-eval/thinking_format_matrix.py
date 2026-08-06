#!/usr/bin/env python3
"""Issue #81 validation — the thinking/format interaction on ``/api/chat``.

The original measurement used ``/api/generate`` with one model (qwen3.5:9b) and
found that ``format: "json"`` + thinking-on drives the JSON constraint onto the
THINKING channel, leaving ``response`` empty. The council does not use
``/api/generate`` — ``gateway_local._ollama_dispatch`` posts to ``/api/chat`` —
and it never sends ``format`` or ``think`` at all. So three things need
confirming before the fix generalises:

1. does the same interaction occur on ``/api/chat``?
2. does it occur on thinking models OTHER than qwen3.5?
3. what does the COUNCIL actually receive, given that
   ``gateway_local.py`` substitutes ``THINKING_TRACE_MARKER + thinking`` when
   ``content`` is empty (so the council's symptom is not an empty string)?

Runs the 2x2 (think on/off x format on/off) per model and reports, per cell:
where the payload landed, whether it parses as JSON, and whether it matches the
requested schema. Usage:

    python3 thinking_format_matrix.py [--trials N] MODEL [MODEL ...]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:11434"

# A reviewer-verdict turn, the same shape the council's REVIEWER role emits.
SCHEMA_HINT = (
    'Reply with ONLY this JSON object and nothing else: '
    '{"approved": <true|false>, "findings": [<string>, ...]}'
)
PROMPT = (
    "You are a code reviewer. The diff adds a function `add(a, b)` that returns "
    "`a - b`, while its docstring says it returns the sum.\n\n" + SCHEMA_HINT
)


def chat(model: str, *, fmt: bool, think: bool | None, timeout: int = 180) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 512},
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
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "__body__": e.read()[:300].decode("utf8", "replace")}
    except Exception as e:  # noqa: BLE001 — a failed trial is data, not a crash
        return {"__error__": f"{type(e).__name__}: {e}"}


def classify(raw: dict) -> dict:
    """Where did the payload land, and is it the schema we asked for?"""
    if "__http_error__" in raw:
        return {"where": f"HTTP {raw['__http_error__']}", "json": False, "schema": False,
                "detail": raw.get("__body__", "")[:120]}
    if "__error__" in raw:
        return {"where": "ERROR", "json": False, "schema": False,
                "detail": raw["__error__"][:120]}
    msg = raw.get("message") or {}
    content = (msg.get("content") or "")
    thinking = (msg.get("thinking") or "")
    where = ("content" if content.strip()
             else "thinking" if thinking.strip() else "EMPTY")
    payload = content if content.strip() else thinking
    ok_json, ok_schema = False, False
    try:
        obj = json.loads(payload)
        ok_json = True
        ok_schema = isinstance(obj, dict) and "approved" in obj and "findings" in obj
    except Exception:  # noqa: BLE001
        pass
    return {"where": where, "json": ok_json, "schema": ok_schema,
            "detail": payload.strip()[:120].replace("\n", " ")}


def council_sees(raw: dict) -> str:
    """Replicate gateway_local._ollama_dispatch's content selection, so the row
    reports what the COUNCIL gets — not what Ollama returned."""
    msg = raw.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        return "FATAL malformed_response: missing_content"
    if not content.strip():
        thinking = msg.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            return "MARKER+thinking"   # THINKING_TRACE_MARKER substitution
        return "empty string"
    return "content"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()

    for model in args.models:
        print(f"\n=== {model} ===")
        print(f"{'think':>6} {'format':>7} | {'landed':>9} {'json':>5} {'schema':>7} "
              f"| {'council receives':>22} | sample")
        print("-" * 118)
        for think in (None, False):
            for fmt in (False, True):
                lands, jsons, schemas, sees, sample = [], 0, 0, [], ""
                for _ in range(args.trials):
                    raw = chat(model, fmt=fmt, think=think)
                    c = classify(raw)
                    lands.append(c["where"])
                    jsons += int(c["json"])
                    schemas += int(c["schema"])
                    sees.append(council_sees(raw))
                    sample = sample or c["detail"]
                t = args.trials
                land = max(set(lands), key=lands.count)
                see = max(set(sees), key=sees.count)
                tl = "default" if think is None else str(think)
                print(f"{tl:>6} {str(fmt):>7} | {land:>9} {jsons:>2}/{t} {schemas:>4}/{t} "
                      f"| {see:>22} | {sample[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
