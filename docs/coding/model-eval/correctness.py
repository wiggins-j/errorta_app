#!/usr/bin/env python3
"""Python code-correctness harness for Errorta model selection.

Each task gives the model a precise signature + spec. The generated code is
executed against a HIDDEN pytest suite the model never sees. Scoring is
execution-based: tests passed / tests total, plus a strict all-or-nothing
task-pass rate. Multiple trials per task measure consistency, not just peak.

Tasks are drawn from work Errorta/AIAR actually does: chunking, rank fusion,
task DAGs, frontmatter parsing, retry logic.

Generated code runs in a temp dir under a subprocess with CPU/address-space
limits and a hard timeout.
"""
import json, re, subprocess, sys, tempfile, textwrap, urllib.request
from pathlib import Path

OLLAMA = "http://localhost:11434"
MODELS = [
    ("qwen2.5-coder:7b", False),
    ("qwen2.5-coder:14b", False),
    ("qwen3.5:9b", True),      # thinking model -> think=false
]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
TIMEOUT_S = 15

TASKS = [
    {
        "name": "parse_duration",
        "spec": '''
Write a function with this exact signature:

    def parse_duration(s: str) -> int:

It parses a human duration string into a whole number of SECONDS.

Rules:
- Supported units: "d" (days), "h" (hours), "m" (minutes), "s" (seconds).
- A string is a sequence of one or more <integer><unit> parts, e.g. "1h30m",
  "2d4h", "45s", "90m".
- Parts may appear in any order and a unit may appear at most once.
- Surrounding whitespace is ignored. The string is case-insensitive.
- An empty/whitespace-only string, a repeated unit, an unknown unit, a
  negative number, or any malformed input raises ValueError.
- A bare integer with no unit raises ValueError.
''',
        "tests": '''
import pytest
from solution import parse_duration

def test_basic_units():
    assert parse_duration("45s") == 45
    assert parse_duration("90m") == 5400
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400

def test_combined():
    assert parse_duration("1h30m") == 5400
    assert parse_duration("2d4h") == 187200
    assert parse_duration("1d2h3m4s") == 93784

def test_order_and_case():
    assert parse_duration("30m1h") == 5400
    assert parse_duration("1H30M") == 5400
    assert parse_duration("  1h30m  ") == 5400

def test_zero():
    assert parse_duration("0s") == 0

def test_errors():
    for bad in ["", "   ", "10", "5x", "1h1h", "-5s", "h", "1.5h", "abc"]:
        with pytest.raises(ValueError):
            parse_duration(bad)
''',
    },
    {
        "name": "chunk_text",
        "spec": '''
Write a function with this exact signature:

    def chunk_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:

It splits text into chunks for a retrieval pipeline.

Rules:
- Each chunk is at most max_chars characters.
- Consecutive chunks overlap by exactly `overlap` characters: chunk i+1 starts
  at (start_of_chunk_i + max_chars - overlap).
- The final chunk may be shorter than max_chars.
- No chunk is ever empty; do not emit a trailing empty chunk.
- An empty string returns [].
- Raise ValueError if max_chars <= 0, if overlap < 0, or if overlap >= max_chars.
- Do not strip or alter the characters; chunks concatenated with overlap
  removed must reconstruct the original text.
''',
        "tests": '''
import pytest
from solution import chunk_text

def test_exact_fit():
    assert chunk_text("abcdef", 3) == ["abc", "def"]

def test_short_tail():
    assert chunk_text("abcdefg", 3) == ["abc", "def", "g"]

def test_smaller_than_max():
    assert chunk_text("ab", 10) == ["ab"]

def test_empty():
    assert chunk_text("", 5) == []

def test_overlap():
    assert chunk_text("abcdefgh", 4, 2) == ["abcd", "cdef", "efgh"]

def test_overlap_tail_not_empty():
    out = chunk_text("abcdefghi", 4, 2)
    assert all(c for c in out)
    assert out[0] == "abcd"

def test_reconstruct_no_overlap():
    text = "the quick brown fox jumps"
    assert "".join(chunk_text(text, 7)) == text

def test_errors():
    for args in [("abc", 0), ("abc", -1)]:
        with pytest.raises(ValueError):
            chunk_text(*args)
    with pytest.raises(ValueError):
        chunk_text("abc", 3, 3)
    with pytest.raises(ValueError):
        chunk_text("abc", 3, -1)
''',
    },
    {
        "name": "rrf_fuse",
        "spec": '''
Write a function with this exact signature:

    def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:

It performs Reciprocal Rank Fusion over several ranked lists of document ids.

Rules:
- For each ranking list, a document at 0-based index i contributes 1/(k + i + 1)
  to its score.
- A document's final score is the sum of its contributions across all lists.
- Return (doc_id, score) pairs sorted by score DESCENDING.
- Ties in score are broken by doc_id ASCENDING (lexicographic).
- Documents absent from a list simply contribute nothing from that list.
- An empty `rankings` list returns [].
- Raise ValueError if k <= 0.
- Duplicate ids WITHIN a single ranking list: only the first (best) occurrence
  in that list counts.
''',
        "tests": '''
import pytest
from solution import rrf_fuse

def test_single_list():
    out = rrf_fuse([["a", "b"]], k=60)
    assert [d for d, _ in out] == ["a", "b"]
    assert out[0][1] == pytest.approx(1/61)
    assert out[1][1] == pytest.approx(1/62)

def test_fusion_sums():
    out = dict(rrf_fuse([["a", "b"], ["b", "a"]], k=60))
    assert out["a"] == pytest.approx(1/61 + 1/62)
    assert out["b"] == pytest.approx(1/62 + 1/61)

def test_tie_broken_by_id():
    out = rrf_fuse([["b", "a"], ["a", "b"]], k=60)
    assert [d for d, _ in out] == ["a", "b"]

def test_missing_docs():
    out = dict(rrf_fuse([["a"], ["b"]], k=60))
    assert out["a"] == pytest.approx(1/61)
    assert out["b"] == pytest.approx(1/61)

def test_ordering_desc():
    out = rrf_fuse([["x", "y", "z"], ["x", "z", "y"]])
    assert [d for d, _ in out][0] == "x"
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True)

def test_empty():
    assert rrf_fuse([]) == []

def test_dup_within_list():
    out = dict(rrf_fuse([["a", "a", "b"]], k=60))
    assert out["a"] == pytest.approx(1/61)

def test_bad_k():
    with pytest.raises(ValueError):
        rrf_fuse([["a"]], k=0)
''',
    },
    {
        "name": "topo_sort_tasks",
        "spec": '''
Write a function with this exact signature:

    def topo_sort_tasks(deps: dict[str, list[str]]) -> list[str]:

It orders tasks so every task appears after all tasks it depends on.

Rules:
- `deps` maps task_id -> list of task_ids it DEPENDS ON (its prerequisites).
- Return a list of every task id, prerequisites first.
- Among tasks that become available at the same time, choose the
  lexicographically smallest id first (deterministic output).
- A dependency id that never appears as a key is still a real task and must
  appear in the output.
- Raise ValueError if the graph contains a cycle.
- An empty dict returns [].
''',
        "tests": '''
import pytest
from solution import topo_sort_tasks

def test_linear():
    assert topo_sort_tasks({"b": ["a"], "a": []}) == ["a", "b"]

def test_deterministic_tie():
    assert topo_sort_tasks({"a": [], "b": [], "c": []}) == ["a", "b", "c"]

def test_diamond():
    out = topo_sort_tasks({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []})
    assert out == ["a", "b", "c", "d"]

def test_implicit_task():
    out = topo_sort_tasks({"b": ["a"]})
    assert out == ["a", "b"]

def test_empty():
    assert topo_sort_tasks({}) == []

def test_cycle():
    with pytest.raises(ValueError):
        topo_sort_tasks({"a": ["b"], "b": ["a"]})

def test_self_cycle():
    with pytest.raises(ValueError):
        topo_sort_tasks({"a": ["a"]})

def test_larger():
    deps = {"deploy": ["test"], "test": ["build"], "build": ["lint", "fetch"],
            "lint": [], "fetch": []}
    out = topo_sort_tasks(deps)
    assert out.index("fetch") < out.index("build")
    assert out.index("lint") < out.index("build")
    assert out[-1] == "deploy"
''',
    },
    {
        "name": "parse_frontmatter",
        "spec": '''
Write a function with this exact signature:

    def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:

It splits a markdown document into its frontmatter mapping and its body.

Rules:
- Frontmatter is delimited by a line containing exactly "---" as the FIRST line
  of the document, and closed by the next line containing exactly "---".
- Inside, each non-empty line is "key: value". Split on the FIRST colon only.
- Keys and values are stripped of surrounding whitespace.
- Blank lines inside the frontmatter are skipped.
- Return (mapping, body). The body is everything after the closing delimiter,
  with ONE leading newline removed if present. Body is otherwise unaltered.
- If the document does not start with "---", return ({}, text) unchanged.
- If the opening "---" is never closed, raise ValueError.
- A line inside the frontmatter with no colon raises ValueError.
''',
        "tests": '''
import pytest
from solution import parse_frontmatter

def test_basic():
    meta, body = parse_frontmatter("---\\nname: foo\\ntype: bar\\n---\\nhello")
    assert meta == {"name": "foo", "type": "bar"}
    assert body == "hello"

def test_strips_whitespace():
    meta, _ = parse_frontmatter("---\\n  name :   foo  \\n---\\n")
    assert meta["name"] == "foo"

def test_first_colon_only():
    meta, _ = parse_frontmatter("---\\nurl: http://x.com/a\\n---\\n")
    assert meta["url"] == "http://x.com/a"

def test_blank_lines_skipped():
    meta, _ = parse_frontmatter("---\\na: 1\\n\\nb: 2\\n---\\n")
    assert meta == {"a": "1", "b": "2"}

def test_no_frontmatter():
    meta, body = parse_frontmatter("just text")
    assert meta == {}
    assert body == "just text"

def test_body_preserved():
    _, body = parse_frontmatter("---\\na: 1\\n---\\nline1\\nline2\\n")
    assert body == "line1\\nline2\\n"

def test_empty_body():
    meta, body = parse_frontmatter("---\\na: 1\\n---\\n")
    assert body == ""

def test_unclosed():
    with pytest.raises(ValueError):
        parse_frontmatter("---\\na: 1\\nno close")

def test_bad_line():
    with pytest.raises(ValueError):
        parse_frontmatter("---\\nnotakeyvalue\\n---\\n")
''',
    },
    {
        "name": "token_bucket",
        "spec": '''
Write a class with this exact interface:

    class TokenBucket:
        def __init__(self, capacity: float, refill_rate: float, clock=None): ...
        def allow(self, cost: float = 1.0) -> bool: ...
        def retry_after(self, cost: float = 1.0) -> float: ...

A classic token-bucket rate limiter.

Rules:
- `clock` is a zero-argument callable returning a float "now" in seconds. If
  None, default to time.monotonic. Never call time.sleep.
- The bucket starts FULL (capacity tokens).
- Tokens refill continuously at refill_rate tokens/second, capped at capacity.
- allow(cost) refills based on elapsed time, then: if available >= cost,
  subtract cost and return True; otherwise subtract nothing and return False.
- retry_after(cost) returns the number of seconds until `cost` tokens would be
  available. It must NOT consume tokens and must NOT advance state. Returns 0.0
  if cost tokens are already available.
- Raise ValueError if capacity <= 0 or refill_rate <= 0.
- Raise ValueError from allow/retry_after if cost > capacity (unsatisfiable).
''',
        "tests": '''
import pytest
from solution import TokenBucket

class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, d): self.t += d

def test_starts_full():
    c = Clock(); b = TokenBucket(3, 1, clock=c)
    assert b.allow() and b.allow() and b.allow()
    assert not b.allow()

def test_refill():
    c = Clock(); b = TokenBucket(2, 1, clock=c)
    assert b.allow() and b.allow()
    assert not b.allow()
    c.advance(1.0)
    assert b.allow()

def test_cap_at_capacity():
    c = Clock(); b = TokenBucket(2, 1, clock=c)
    c.advance(100)
    assert b.allow() and b.allow()
    assert not b.allow()

def test_retry_after_zero_when_available():
    c = Clock(); b = TokenBucket(2, 1, clock=c)
    assert b.retry_after() == pytest.approx(0.0)

def test_retry_after_value():
    c = Clock(); b = TokenBucket(1, 2.0, clock=c)
    assert b.allow()
    assert b.retry_after(1) == pytest.approx(0.5)

def test_retry_after_does_not_consume():
    c = Clock(); b = TokenBucket(1, 1, clock=c)
    b.retry_after(); b.retry_after()
    assert b.allow()

def test_rejected_does_not_consume():
    c = Clock(); b = TokenBucket(1, 1, clock=c)
    assert b.allow()
    assert not b.allow(1)
    c.advance(1.0)
    assert b.allow()

def test_cost():
    c = Clock(); b = TokenBucket(5, 1, clock=c)
    assert b.allow(3)
    assert not b.allow(3)
    assert b.allow(2)

def test_errors():
    with pytest.raises(ValueError): TokenBucket(0, 1)
    with pytest.raises(ValueError): TokenBucket(1, 0)
    c = Clock(); b = TokenBucket(2, 1, clock=c)
    with pytest.raises(ValueError): b.allow(3)
    with pytest.raises(ValueError): b.retry_after(3)
''',
    },
    {
        "name": "merge_ranges",
        "spec": '''
Write a function with this exact signature:

    def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:

It merges overlapping half-open line ranges [start, end).

Rules:
- Each input tuple is (start, end) with start < end, half-open.
- Merge ranges that overlap OR that touch exactly (end == next start).
- Return the merged ranges sorted by start ascending.
- Input is not necessarily sorted, and may be empty (return []).
- Do not mutate the input list.
- Raise ValueError if any range has start >= end.
''',
        "tests": '''
import pytest
from solution import merge_ranges

def test_empty():
    assert merge_ranges([]) == []

def test_no_overlap():
    assert merge_ranges([(1, 2), (5, 7)]) == [(1, 2), (5, 7)]

def test_overlap():
    assert merge_ranges([(1, 5), (3, 8)]) == [(1, 8)]

def test_touching_merges():
    assert merge_ranges([(1, 3), (3, 5)]) == [(1, 5)]

def test_unsorted():
    assert merge_ranges([(5, 7), (1, 3)]) == [(1, 3), (5, 7)]

def test_contained():
    assert merge_ranges([(1, 10), (3, 4)]) == [(1, 10)]

def test_chain():
    assert merge_ranges([(1, 3), (2, 5), (4, 9)]) == [(1, 9)]

def test_no_mutation():
    src = [(5, 7), (1, 3)]
    copy = list(src)
    merge_ranges(src)
    assert src == copy

def test_error():
    with pytest.raises(ValueError):
        merge_ranges([(3, 3)])
    with pytest.raises(ValueError):
        merge_ranges([(5, 2)])
''',
    },
    {
        "name": "truncate_middle",
        "spec": '''
Write a function with this exact signature:

    def truncate_middle(s: str, max_len: int, ellipsis: str = "...") -> str:

It shortens a string to at most max_len characters by removing the MIDDLE.

Rules (apply IN THIS ORDER):
1. If len(s) <= max_len, return s unchanged. This check happens FIRST, before
   any validation of max_len.
2. Otherwise, truncation is needed: raise ValueError if max_len <= len(ellipsis).
3. Otherwise return head + ellipsis + tail, total length EXACTLY max_len.
   The head gets the extra character when the remaining space is odd:
   available = max_len - len(ellipsis); head = ceil(available/2);
   tail = available - head
''',
        "tests": '''
import pytest
from solution import truncate_middle

def test_short_unchanged():
    assert truncate_middle("abc", 10) == "abc"
    assert truncate_middle("abc", 3) == "abc"

def test_exact_length():
    out = truncate_middle("abcdefghij", 7)
    assert len(out) == 7
    assert out == "ab...ij"

def test_odd_extra_to_head():
    out = truncate_middle("abcdefghij", 8)
    assert len(out) == 8
    assert out == "abc...ij"

def test_custom_ellipsis():
    out = truncate_middle("abcdefghij", 5, "*")
    assert len(out) == 5
    assert out == "ab*ij"

def test_error():
    with pytest.raises(ValueError):
        truncate_middle("abcdef", 3)
    with pytest.raises(ValueError):
        truncate_middle("abcdef", 2)
''',
    },
]

PROMPT = """You are writing production Python. Output ONLY a single ```python code block.
No explanation before or after the block.

{spec}

Requirements:
- Include all imports the code needs.
- Use type hints.
- Do not include tests, examples, __main__ blocks, or print statements.
- The code must be importable as a module named `solution`.
"""

RUNNER = """
import resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
import pytest
sys.exit(pytest.main(["-q", "--no-header", "-p", "no:cacheprovider", "test_solution.py"]))
"""


def extract_code(text: str) -> str:
    m = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return max(m, key=len)
    return text


def generate(model, think_off, spec):
    body = {
        "model": model,
        "prompt": PROMPT.format(spec=spec),
        "stream": False,
        "options": {"num_ctx": 8192, "num_predict": 1600, "temperature": 0.2},
    }
    if think_off:
        body["think"] = False
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    return d.get("response", "") or d.get("thinking", "")


def run_tests(code, tests):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "solution.py").write_text(code)
        (p / "test_solution.py").write_text(textwrap.dedent(tests))
        (p / "run.py").write_text(RUNNER)
        try:
            proc = subprocess.run(
                [sys.executable, "run.py"], cwd=td, capture_output=True,
                text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return 0, 0, "timeout"
        out = proc.stdout + proc.stderr
        m = re.search(r"(\d+) passed", out)
        passed = int(m.group(1)) if m else 0
        m = re.search(r"(\d+) failed", out)
        failed = int(m.group(1)) if m else 0
        if "error" in out.lower() and passed == 0 and failed == 0:
            return 0, 0, "import_or_collect_error"
        return passed, passed + failed, "ok" if proc.returncode == 0 else "failures"


def main():
    results = {}
    for model, think_off in MODELS:
        agg = {"tasks": {}, "tests_passed": 0, "tests_total": 0,
               "task_pass": 0, "task_runs": 0}
        for task in TASKS:
            per = []
            for t in range(TRIALS):
                try:
                    raw = generate(model, think_off, task["spec"])
                except Exception as exc:
                    per.append({"trial": t, "status": f"gen_error:{type(exc).__name__}",
                                "passed": 0, "total": 0})
                    agg["task_runs"] += 1
                    continue
                code = extract_code(raw)
                passed, total, status = run_tests(code, task["tests"])
                expected = task["tests"].count("def test_")
                total = total or expected
                per.append({"trial": t, "status": status,
                            "passed": passed, "total": expected})
                agg["tests_passed"] += passed
                agg["tests_total"] += expected
                agg["task_runs"] += 1
                if passed == expected:
                    agg["task_pass"] += 1
            agg["tasks"][task["name"]] = per
            best = max(x["passed"] for x in per)
            worst = min(x["passed"] for x in per)
            exp = task["tests"].count("def test_")
            print(f"{model:20} {task['name']:20} "
                  f"best={best}/{exp} worst={worst}/{exp} "
                  f"trials={[x['passed'] for x in per]} "
                  f"status={[x['status'] for x in per]}", flush=True)
        results[model] = agg
        pct = 100.0 * agg["tests_passed"] / max(1, agg["tests_total"])
        tpct = 100.0 * agg["task_pass"] / max(1, agg["task_runs"])
        print(f"==> {model}: tests {agg['tests_passed']}/{agg['tests_total']} "
              f"({pct:.1f}%)  full-task pass {agg['task_pass']}/{agg['task_runs']} "
              f"({tpct:.1f}%)", flush=True)
    print("###CORRECTNESS_SUMMARY###")
    print(json.dumps(results, indent=2))
    print("CORRECTNESS_COMPLETE")


if __name__ == "__main__":
    main()
