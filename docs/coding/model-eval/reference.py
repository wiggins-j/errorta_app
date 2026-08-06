"""Reference solutions used ONLY to validate the hidden test suites."""
import math, re, time
from typing import Callable, Optional

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(s: str) -> int:
    if not isinstance(s, str):
        raise ValueError("not a string")
    t = s.strip().lower()
    if not t:
        raise ValueError("empty")
    if not re.fullmatch(r"(?:\d+[dhms])+", t):
        raise ValueError(f"malformed: {s!r}")
    seen, total = set(), 0
    for num, unit in re.findall(r"(\d+)([dhms])", t):
        if unit in seen:
            raise ValueError(f"repeated unit {unit}")
        seen.add(unit)
        total += int(num) * _UNITS[unit]
    return total


def chunk_text(text: str, max_chars: int, overlap: int = 0) -> list[str]:
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if overlap >= max_chars:
        raise ValueError("overlap must be < max_chars")
    if not text:
        return []
    step = max_chars - overlap
    out, i = [], 0
    while i < len(text):
        piece = text[i:i + max_chars]
        if piece:
            out.append(piece)
        if i + max_chars >= len(text):
            break
        i += step
    return out


def rrf_fuse(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    if k <= 0:
        raise ValueError("k must be > 0")
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for i, doc in enumerate(ranking):
            if doc in seen:
                continue
            seen.add(doc)
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + i + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def topo_sort_tasks(deps: dict[str, list[str]]) -> list[str]:
    nodes = set(deps)
    for prereqs in deps.values():
        nodes.update(prereqs)
    remaining = {n: set(deps.get(n, ())) for n in nodes}
    out: list[str] = []
    while remaining:
        ready = sorted(n for n, p in remaining.items() if not p)
        if not ready:
            raise ValueError("cycle detected")
        pick = ready[0]
        out.append(pick)
        del remaining[pick]
        for p in remaining.values():
            p.discard(pick)
    return out


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---" or lines[0] != "---":
        return {}, text
    close = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            close = i
            break
    if close is None:
        raise ValueError("unclosed frontmatter")
    meta: dict[str, str] = {}
    for line in lines[1:close]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"bad frontmatter line: {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    body = "\n".join(lines[close + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


class TokenBucket:
    def __init__(self, capacity: float, refill_rate: float,
                 clock: Optional[Callable[[], float]] = None):
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._clock = clock or time.monotonic
        self._tokens = float(capacity)
        self._last = self._clock()

    def _peek(self) -> float:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        return min(self.capacity, self._tokens + elapsed * self.refill_rate), now

    def allow(self, cost: float = 1.0) -> bool:
        if cost > self.capacity:
            raise ValueError("cost exceeds capacity")
        tokens, now = self._peek()
        self._tokens, self._last = tokens, now
        if tokens >= cost:
            self._tokens = tokens - cost
            return True
        return False

    def retry_after(self, cost: float = 1.0) -> float:
        if cost > self.capacity:
            raise ValueError("cost exceeds capacity")
        tokens, _ = self._peek()
        if tokens >= cost:
            return 0.0
        return (cost - tokens) / self.refill_rate


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    for s, e in ranges:
        if s >= e:
            raise ValueError(f"invalid range ({s}, {e})")
    if not ranges:
        return []
    out: list[tuple[int, int]] = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def truncate_middle(s: str, max_len: int, ellipsis: str = "...") -> str:
    if len(s) <= max_len:
        return s
    if max_len <= len(ellipsis):
        raise ValueError("max_len must exceed ellipsis length")
    available = max_len - len(ellipsis)
    head = math.ceil(available / 2)
    tail = available - head
    return s[:head] + ellipsis + (s[len(s) - tail:] if tail else "")
