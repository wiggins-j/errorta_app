#!/usr/bin/env python3
"""Fair re-test of qwen3.5:9b — it is a THINKING model, so score `response`
with a fallback to `thinking`, and also try think=false."""
import json, re, urllib.request

OLLAMA = "http://localhost:11434"
MODEL = "qwen3.5:9b"
TRIALS = 6

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


def validate(obj):
    # Accept the bare schema, or the {"content": {...}} envelope qwen3.5 wraps it in.
    if isinstance(obj, dict) and "verdict" not in obj and isinstance(obj.get("content"), dict):
        obj = obj["content"]
    if not isinstance(obj, dict):
        return False, "not_object"
    if obj.get("verdict") not in ("approve", "request_changes"):
        return False, f"bad_verdict:{obj.get('verdict')!r}"
    if not isinstance(obj.get("findings"), list):
        return False, "findings_not_list"
    return True, "ok"


def extract(text):
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    for pat in (r"```(?:json)?\s*(\{.*?\})\s*```", r"\{.*\}"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(1) if m.lastindex else m.group(0))
            except Exception:
                continue
    return None


def call(force_json, think):
    body = {"model": MODEL, "prompt": REVIEWER, "stream": False,
            "options": {"num_ctx": 8192, "num_predict": 800, "temperature": 0.3}}
    if force_json:
        body["format"] = "json"
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return d.get("response", ""), d.get("thinking", "")


for label, force_json, think in (
    ("raw,   think=default", False, None),
    ("json,  think=default", True, None),
    ("raw,   think=false", False, False),
    ("json,  think=false", True, False),
):
    ok = resp_ok = think_only = 0
    reasons = {}
    for _ in range(TRIALS):
        try:
            resp, thk = call(force_json, think)
        except Exception as exc:
            reasons[f"http:{type(exc).__name__}"] = reasons.get(f"http:{type(exc).__name__}", 0) + 1
            continue
        obj = extract(resp)
        used_thinking = False
        if obj is None:
            obj = extract(thk)
            used_thinking = obj is not None
        if obj is None:
            reasons["unparseable"] = reasons.get("unparseable", 0) + 1
            continue
        good, why = validate(obj)
        if good:
            ok += 1
            if used_thinking:
                think_only += 1
            else:
                resp_ok += 1
        else:
            reasons[why] = reasons.get(why, 0) + 1
    print(f"{label:22} valid={ok}/{TRIALS}  in_response={resp_ok}  "
          f"ONLY_in_thinking={think_only}  {reasons}", flush=True)
print("RETEST_COMPLETE")
