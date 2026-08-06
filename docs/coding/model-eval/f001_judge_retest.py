#!/usr/bin/env python3
"""F001 judge-schema re-test — the attribution run SPEC-42 gates on.

F001 records `qwen3.5:9b` "emitting wrong-schema JSON" in a judge seat and
proposes a 15 GB `mistral-small3.1` judge instead. SPEC-42 §"Only after both"
says that observation may be an artefact of defect 1 (empty model id), defect 2
(2048-token budget vs a 2197-token reasoning mean), or neither — and that it
must not be re-measured until both fixes land. Both are now in main.

Defect 1 is a dispatch bug, not a model behaviour: an empty model id is an
HTTP 400 before the model is ever reached, so it cannot produce wrong-schema
JSON and is not an arm here. What IS testable is defect 2 and SPEC-41.

Three arms, same prompts, same seeds-per-trial count, on the council's REAL
egress path (`/api/chat`, not `/api/generate`):

  A  f001        num_predict=2048, thinking on,  no format   <- pre-fix council
  B  budget      num_predict=8192, thinking on,  no format   <- SPEC-42 alone
  C  shipped     num_predict=8192, think:false,  format=json <- what main does

Reading the result:
  * A red, C green  -> F001's observation was an artefact; the judge swap is
                       unfounded on the coding path.
  * A red, B red, C green -> it was the output CHANNEL, not the budget.
  * A red, C red    -> F001 stands on its own merits, and truncation must stop
                       being cited as the explanation.

Both outcomes were written down before the run. Run on senditai only.
"""
import json
import re
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
JUDGE = "qwen3.5:9b"
# The F001 recommendation, measured under the same arms so the comparison the
# spec actually cares about — "is the 15 GB swap worth it?" — has both sides.
CONTROL = "mistral-small3.1:latest"

REVIEWER = (
    "Review this Python diff and reply with ONLY a JSON object, no prose:\n"
    '{"verdict": "approve" | "request_changes", '
    '"findings": [{"severity": "high"|"medium"|"low", "file": str, "summary": str}]}\n\n'
    "```python\n"
    "def allow(self, key):\n"
    "    b = self.buckets.get(key)\n"
    "    if b is None:\n"
    "        b = self.buckets[key] = [self.capacity, time.time()]\n"
    "    tokens, last = b\n"
    "    tokens = min(self.capacity, tokens + (time.time()-last)*self.rate)\n"
    "    if tokens < 1:\n"
    "        return False\n"
    "    b[0] = tokens - 1\n"
    "    return True\n"
    "```\n"
)

ARMS = (
    # (name, num_predict, think, format_json)
    ("A_f001", 2048, True, False),
    ("B_budget", 8192, True, False),
    ("C_shipped", 8192, False, True),
)


def validate_reviewer(obj):
    if not isinstance(obj, dict):
        return False, "not_object"
    if obj.get("verdict") not in ("approve", "request_changes"):
        return False, f"bad_verdict:{obj.get('verdict')!r}"
    findings = obj.get("findings")
    if not isinstance(findings, list):
        return False, "findings_not_list"
    for item in findings:
        if not isinstance(item, dict):
            return False, "finding_not_object"
        if item.get("severity") not in ("high", "medium", "low"):
            return False, f"bad_severity:{item.get('severity')!r}"
        if not isinstance(item.get("summary"), str):
            return False, "summary_not_str"
    return True, "ok"


def extract(text):
    """Council-style tolerant parse: whole body, then fenced block, then first {...}."""
    try:
        return json.loads(text.strip()), "direct"
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1)), "fenced"
        except Exception:
            pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0)), "embedded"
        except Exception:
            pass
    return None, "unparseable"


def call(model, prompt, num_predict, think, format_json):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": 8192, "num_predict": num_predict, "temperature": 0.3},
    }
    if not think:
        body["think"] = False
    if format_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
    return (
        (data.get("message") or {}).get("content") or "",
        data.get("done_reason") or "",
        int(data.get("eval_count") or 0),
    )


def main():
    results = {}
    for model in (JUDGE, CONTROL):
        results[model] = {}
        for name, num_predict, think, format_json in ARMS:
            ok = truncated = 0
            direct = 0
            tokens = []
            reasons = {}
            for _ in range(TRIALS):
                try:
                    text, done_reason, eval_count = call(
                        model, REVIEWER, num_predict, think, format_json
                    )
                except Exception as exc:
                    key = f"http:{type(exc).__name__}"
                    reasons[key] = reasons.get(key, 0) + 1
                    continue
                tokens.append(eval_count)
                if done_reason == "length":
                    truncated += 1
                obj, how = extract(text)
                if how == "direct":
                    direct += 1
                if obj is None:
                    reasons["unparseable"] = reasons.get("unparseable", 0) + 1
                    continue
                good, why = validate_reviewer(obj)
                if good:
                    ok += 1
                else:
                    reasons[why] = reasons.get(why, 0) + 1
            mean = round(sum(tokens) / len(tokens)) if tokens else 0
            results[model][name] = {
                "schema_ok": f"{ok}/{TRIALS}",
                "truncated": f"{truncated}/{TRIALS}",
                "direct_parse": f"{direct}/{TRIALS}",
                "mean_eval_tokens": mean,
                "failures": reasons,
            }
            print(f"{model:26} {name:11} {results[model][name]}", flush=True)
    print("\n=== JSON ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
