#!/usr/bin/env python3
"""Role-aware soak test for the Errorta coding council.

Drives 2 concurrent agents (matching "two developer agents at once") through
PM / DEV / REVIEWER / TESTER shaped prompts, while sampling GPU power, temp,
utilisation and VRAM. Reports throughput, stability and any CPU spill.
"""
import json, subprocess, sys, threading, time, urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
HW = "/sys/class/drm/card1/device/hwmon/hwmon4"
DEV_DIR = "/sys/class/drm/card1/device"
NUM_CTX = int(sys.argv[2]) if len(sys.argv) > 2 else 16384
MINUTES = float(sys.argv[3]) if len(sys.argv) > 3 else 7.0
MODEL = sys.argv[1]

# Role-shaped prompts mirroring errorta_council/coding/topology.py roles and
# the ROLE_SKILLS directives in skills.py.
ROLES = {
    "pm": (
        "You are the PM of a coding team. Turn this goal into bite-sized, "
        "independently-testable tasks before any code is written: 'Add rate "
        "limiting to a FastAPI service with per-API-key quotas, Redis-backed "
        "counters, and a 429 response carrying Retry-After.' Output a numbered "
        "task list. Each task needs: title, difficulty tier (light/mid/strong), "
        "acceptance criteria, and its dependencies on other tasks."
    ),
    "dev": (
        "Write a failing test FIRST, then the minimal code to pass it. Task: "
        "implement a token-bucket rate limiter class in Python with methods "
        "allow(key) -> bool and retry_after(key) -> float. Backed by an "
        "injectable clock and an injectable store. Full type hints and "
        "docstrings. Give the pytest test module first, then the implementation."
    ),
    "reviewer": (
        "Produce a structured verdict (approve / request_changes) with concrete, "
        "actionable findings — never a vague 'looks fine'. Review this diff:\n\n"
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
        "Reply as JSON: {\"verdict\": ..., \"findings\": [{\"severity\": ..., "
        "\"file\": ..., \"summary\": ...}]}"
    ),
    "tester": (
        "Run the code and confirm it actually does what it should before marking "
        "it done. For the token-bucket limiter above, enumerate the edge cases a "
        "verification pass must cover, and for each give the concrete input, the "
        "expected output, and how you would observe it. Include concurrency and "
        "clock-skew cases."
    ),
}
ORDER = ["pm", "dev", "reviewer", "tester"]


def read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return -1


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.rows = []

    def run(self):
        while not self.stop.is_set():
            self.rows.append((
                read_int(f"{HW}/power1_average") / 1e6,
                read_int(f"{HW}/temp1_input") / 1000.0,
                read_int(f"{DEV_DIR}/gpu_busy_percent"),
                read_int(f"{DEV_DIR}/mem_info_vram_used") / 2**30,
                read_int(f"{HW}/fan1_input"),
            ))
            time.sleep(2.0)


def generate(role, prompt):
    body = json.dumps({
        "model": MODEL, "prompt": prompt, "stream": False,
        "options": {"num_ctx": NUM_CTX, "num_predict": 600},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        d = json.loads(resp.read())
    return {
        "role": role,
        "wall_s": round(time.time() - t0, 2),
        "gen_toks": d.get("eval_count", 0),
        "gen_tps": round(d["eval_count"] / (d["eval_duration"] / 1e9), 1) if d.get("eval_duration") else 0,
        "prompt_toks": d.get("prompt_eval_count", 0),
        "prompt_tps": round(d.get("prompt_eval_count", 0) / (d.get("prompt_eval_duration", 1) / 1e9), 1),
        "load_s": round(d.get("load_duration", 0) / 1e9, 2),
    }


def agent(name, deadline, out, errors):
    i = 0
    while time.time() < deadline:
        role = ORDER[i % len(ORDER)]
        try:
            r = generate(role, ROLES[role])
            r["agent"] = name
            out.append(r)
        except Exception as exc:
            errors.append(f"{name}/{role}: {type(exc).__name__}: {exc}")
        i += 1


def main():
    print(f"### MODEL={MODEL}  num_ctx={NUM_CTX}  minutes={MINUTES}", flush=True)
    subprocess.run(["ollama", "stop", MODEL], capture_output=True)
    time.sleep(2)

    sampler = Sampler(); sampler.start()
    results, errors = [], []
    deadline = time.time() + MINUTES * 60
    threads = [threading.Thread(target=agent, args=(f"agent{n}", deadline, results, errors))
               for n in (1, 2)]
    for t in threads: t.start()

    # capture placement mid-run (after the model is warm)
    time.sleep(45)
    ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout

    for t in threads: t.join()
    sampler.stop.set(); sampler.join(timeout=5)

    rows = [r for r in sampler.rows if r[0] >= 0]
    pw = [r[0] for r in rows]; tp = [r[1] for r in rows]
    bz = [r[2] for r in rows]; vr = [r[3] for r in rows]; fn = [r[4] for r in rows]
    summary = {
        "model": MODEL, "num_ctx": NUM_CTX, "minutes": MINUTES,
        "requests": len(results), "errors": errors,
        "placement": ps.strip().splitlines()[-1] if ps.strip() else "",
        "power_avg_W": round(sum(pw) / len(pw), 1) if pw else 0,
        "power_peak_W": round(max(pw), 1) if pw else 0,
        "temp_avg_C": round(sum(tp) / len(tp), 1) if tp else 0,
        "temp_peak_C": round(max(tp), 1) if tp else 0,
        "busy_avg_pct": round(sum(bz) / len(bz)) if bz else 0,
        "vram_peak_GiB": round(max(vr), 2) if vr else 0,
        "fan_peak_rpm": max(fn) if fn else 0,
    }
    if results:
        g = [r["gen_tps"] for r in results]
        summary["gen_tps_avg"] = round(sum(g) / len(g), 1)
        summary["gen_tps_min"] = min(g)
        summary["gen_tps_max"] = max(g)
        summary["prompt_tps_avg"] = round(
            sum(r["prompt_tps"] for r in results) / len(results), 1)
        summary["wall_s_avg"] = round(sum(r["wall_s"] for r in results) / len(results), 1)
        summary["wall_s_p95"] = sorted(r["wall_s"] for r in results)[int(len(results) * 0.95) - 1]
        by_role = {}
        for role in ORDER:
            rs = [r for r in results if r["role"] == role]
            if rs:
                by_role[role] = {
                    "n": len(rs),
                    "gen_tps": round(sum(x["gen_tps"] for x in rs) / len(rs), 1),
                    "wall_s": round(sum(x["wall_s"] for x in rs) / len(rs), 1),
                }
        summary["by_role"] = by_role
    print("###SUMMARY###")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
