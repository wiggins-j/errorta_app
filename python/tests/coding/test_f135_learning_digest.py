"""F135 — PM learning digest projection over the shared performance corpus."""
import json
from datetime import datetime, timezone
from pathlib import Path

from errorta_council.coding.model_catalog import load_catalog
from errorta_council.coding.model_selector import (
    DEMOTION_ACCEPTED_RATE,
    MIN_ATTEMPTS_FOR_SIGNAL,
    PREFERRED_ACCEPTED_RATE,
    _effective_rank,
)
from errorta_council.coding.model_tier import tier_rank
from errorta_council.coding.performance_corpus import (
    append,
    digest,
    learning_digest,
    make_attempt,
)

NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
RECENT = "2026-07-01T00:00:00+00:00"


def _write(path: Path, route: str, accepted: int, rejected: int, *,
           project: str = "p1", task_type: str = "implementation",
           difficulty: str = "mid", cost: int = 1, cap: str = "mid") -> None:
    for i in range(accepted):
        append(make_attempt(
            assignment_id=f"a-{route}-{i}", project_id=project, run_id="r",
            task_id=f"t-a-{route}-{i}", member_id="m", route_id=route,
            task_type=task_type, difficulty_tier=difficulty, capability_tier=cap,
            cost_tier=cost, latency_ms=100, outcome="accepted", started_at=RECENT,
        ), path)
    for i in range(rejected):
        append(make_attempt(
            assignment_id=f"r-{route}-{i}", project_id=project, run_id="r",
            task_id=f"t-r-{route}-{i}", member_id="m", route_id=route,
            task_type=task_type, difficulty_tier=difficulty, capability_tier=cap,
            cost_tier=cost, latency_ms=100, outcome="rejected", started_at=RECENT,
        ), path)


def _bucket(ld: dict, route: str, tt_diff: str) -> dict | None:
    for r in ld["routes"]:
        if r["route_id"] == route:
            for b in r["buckets"]:
                if f"{b['task_type']}:{b['difficulty_tier']}" == tt_diff:
                    return b
    return None


def test_standings_are_four_way_at_the_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    _write(path, "r.insufficient", 2, 2)          # 4 attempts -> insufficient
    _write(path, "r.demoted", 2, 8)               # 0.20 -> demoted
    _write(path, "r.boundary_cautioned", 6, 4)    # exactly 0.60 -> cautioned (not demoted)
    _write(path, "r.cautioned", 7, 3)             # 0.70 -> cautioned
    _write(path, "r.boundary_preferred", 8, 2)    # exactly 0.80 -> preferred
    _write(path, "r.preferred", 9, 1)             # 0.90 -> preferred
    ld = learning_digest(path, now=NOW)
    assert _bucket(ld, "r.insufficient", "implementation:mid")["standing"] == "insufficient_data"
    assert _bucket(ld, "r.demoted", "implementation:mid")["standing"] == "demoted"
    assert _bucket(ld, "r.boundary_cautioned", "implementation:mid")["standing"] == "cautioned"
    assert _bucket(ld, "r.cautioned", "implementation:mid")["standing"] == "cautioned"
    assert _bucket(ld, "r.boundary_preferred", "implementation:mid")["standing"] == "preferred"
    assert _bucket(ld, "r.preferred", "implementation:mid")["standing"] == "preferred"


def test_summary_and_thresholds(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 4, 1)
    _write(path, "claude_cli.haiku", 3, 2, difficulty="light")
    ld = learning_digest(path, now=NOW)
    summary = ld["summary"]
    assert summary["total_attempts"] == 10
    assert summary["distinct_routes"] == 2
    assert summary["window_days"] == 90
    assert summary["corpus_available"] is True
    assert ld["thresholds"] == {
        "min_attempts": MIN_ATTEMPTS_FOR_SIGNAL,
        "demotion_rate": DEMOTION_ACCEPTED_RATE,
        "preferred_rate": PREFERRED_ACCEPTED_RATE,
    }


def test_cold_start_missing_corpus_is_fail_open(tmp_path: Path) -> None:
    ld = learning_digest(tmp_path / "nope.jsonl", now=NOW)
    assert ld["summary"]["corpus_available"] is False
    assert ld["routes"] == []
    assert ld["summary"]["total_attempts"] == 0


def test_malformed_corpus_is_fail_open(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    path.write_text("{truncated\nnot json\n", encoding="utf-8")
    ld = learning_digest(path, now=NOW)
    assert ld["summary"]["corpus_available"] is False
    assert ld["routes"] == []


def test_digest_is_global_never_project_scoped(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 3, 0, project="project-a")
    _write(path, "claude_cli.sonnet", 2, 0, project="project-b")
    ld = learning_digest(path, now=NOW)
    bucket = _bucket(ld, "claude_cli.sonnet", "implementation:mid")
    # Both projects' attempts land in the one shared bucket.
    assert bucket["attempts"] == 5


def test_payload_carries_no_task_content(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 5, 0)
    blob = json.dumps(learning_digest(path, now=NOW))
    for forbidden in ("prompt", "response", "body", "content", "detail"):
        assert forbidden not in blob


def test_demoted_standing_implies_selector_downgrade(tmp_path: Path) -> None:
    """Core decision 3: the explanation cannot drift from the behavior.

    A concrete bucket the projection labels ``demoted`` must be one the selector
    actually downgrades via ``_effective_rank``."""
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 2, 8)  # 10 attempts, 0.20 -> demoted
    ld = learning_digest(path, now=NOW)
    assert _bucket(ld, "claude_cli.sonnet", "implementation:mid")["standing"] == "demoted"

    d = digest(path, now=NOW)
    entry = load_catalog(["claude_cli.sonnet"])["claude_cli.sonnet"]
    base = tier_rank(entry.capability_tier)
    effective, _penalty = _effective_rank(entry, "implementation", "mid", d)
    assert effective == max(0, base - 1)  # selector demoted it a tier


def test_preferred_standing_does_not_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 9, 1)  # 0.90 -> preferred
    d = digest(path, now=NOW)
    entry = load_catalog(["claude_cli.sonnet"])["claude_cli.sonnet"]
    base = tier_rank(entry.capability_tier)
    effective, _penalty = _effective_rank(entry, "implementation", "mid", d)
    assert effective == base  # no downgrade


def test_digest_output_shape_is_unchanged(tmp_path: Path) -> None:
    """Regression: the selector still reads the fused string-keyed dict."""
    path = tmp_path / "attempts.jsonl"
    _write(path, "claude_cli.sonnet", 3, 1)
    d = digest(path, now=NOW)
    assert set(d) == {"claude_cli.sonnet"}
    assert "implementation:mid" in d["claude_cli.sonnet"]
    stats = d["claude_cli.sonnet"]["implementation:mid"]
    assert stats["attempts"] == 4 and stats["accepted"] == 3
