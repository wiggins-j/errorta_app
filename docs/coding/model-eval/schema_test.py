#!/usr/bin/env python3
"""Structured-output compliance test for Errorta's coding council.

The council does NOT use Ollama constrained decoding (no `format` param) — it
relies on prompt-based schema adherence. F001 records qwen3.5:9b emitting
wrong-schema JSON. This measures, per model:

  * raw   — current council behaviour (prompt only)
  * json  — with Ollama `format: "json"` (candidate fix)

for the REVIEWER verdict and the PM task-decomposition shapes.
"""
import json, re, sys, urllib.request

OLLAMA = "http://localhost:11434"
MODELS = ["qwen3.5:9b", "qwen2.5-coder:7b", "qwen2.5-coder:14b"]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 8

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

PM = (
    "Decompose this goal into tasks. Reply with ONLY a JSON object, no prose:\n"
    '{"tasks": [{"title": str, "difficulty_tier": "light"|"mid"|"strong", '
    '"acceptance": str, "depends_on": [str]}]}\n\n'
    "Goal: add per-API-key rate limiting to a FastAPI service, Redis-backed, "
    "returning 429 with Retry-After.\n"
)


def validate_reviewer(obj):
    if not isinstance(obj, dict):
        return False, "not_object"
    if obj.get("verdict") not in ("approve", "request_changes"):
        return False, f"bad_verdict:{obj.get('verdict')!r}"
    f = obj.get("findings")
    if not isinstance(f, list):
        return False, "findings_not_list"
    for item in f:
        if not isinstance(item, dict):
            return False, "finding_not_object"
        if item.get("severity") not in ("high", "medium", "low"):
            return False, f"bad_severity:{item.get('severity')!r}"
        if not isinstance(item.get("summary"), str):
            return False, "summary_not_str"
    return True, "ok"


def validate_pm(obj):
    if not isinstance(obj, dict):
        return False, "not_object"
    tasks = obj.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "tasks_missing"
    for t in tasks:
        if not isinstance(t, dict):
            return False, "task_not_object"
        if t.get("difficulty_tier") not in ("light", "mid", "strong"):
            return False, f"bad_tier:{t.get('difficulty_tier')!r}"
        if not isinstance(t.get("title"), str):
            return False, "title_not_str"
        if not isinstance(t.get("depends_on"), list):
            return False, "depends_on_not_list"
    return True, "ok"


def extract(text):
    """Council-style tolerant parse: whole body, then fenced block, then first {...}."""
    for candidate in (text, ):
        try:
            return json.loads(candidate.strip()), "direct"
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


def call(model, prompt, force_json):
    opts = {"num_ctx": 8192, "num_predict": 800, "temperature": 0.3}
    body = {"model": model, "prompt": prompt, "stream": False, "options": opts}
    if force_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read()).get("response", "")


def main():
    out = {}
    for model in MODELS:
        out[model] = {}
        for shape, prompt, validator in (
            ("reviewer", REVIEWER, validate_reviewer),
            ("pm", PM, validate_pm),
        ):
            for mode in ("raw", "json"):
                ok = 0
                direct = 0
                reasons = {}
                for _ in range(TRIALS):
                    try:
                        text = call(model, prompt, mode == "json")
                    except Exception as exc:
                        reasons[f"http:{type(exc).__name__}"] = reasons.get(
                            f"http:{type(exc).__name__}", 0) + 1
                        continue
                    obj, how = extract(text)
                    if how == "direct":
                        direct += 1
                    if obj is None:
                        reasons["unparseable"] = reasons.get("unparseable", 0) + 1
                        continue
                    valid, why = validator(obj)
                    if valid:
                        ok += 1
                    else:
                        reasons[why] = reasons.get(why, 0) + 1
                out[model][f"{shape}/{mode}"] = {
                    "valid": f"{ok}/{TRIALS}",
                    "clean_json_no_prose": f"{direct}/{TRIALS}",
                    "failures": reasons,
                }
                print(f"{model:22} {shape:8} {mode:4} valid={ok}/{TRIALS} "
                      f"clean={direct}/{TRIALS} {reasons}", flush=True)
    print("###SCHEMA_SUMMARY###")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
