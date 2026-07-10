"""Tests for errorta_judge.metrics."""
from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path

import pytest

from errorta_judge import metrics


def test_record_verdict_appends_and_returns_id(tmp_errorta_home: Path) -> None:
    eid = metrics.record_verdict(
        prompt="what is 2+2?",
        answer="4",
        verdict={"rating": "pass", "failure_tags": []},
        judge_model="llama3.1:8b",
    )
    assert isinstance(eid, str) and len(eid) == 32
    log = metrics.log_path()
    assert log.exists()
    assert log.read_text(encoding="utf-8").count("\n") == 1


def test_record_acceptance_supersedes_prior(tmp_errorta_home: Path) -> None:
    eid = metrics.record_verdict("p", "a", {"rating": "fail"}, None)
    entry = metrics.record_acceptance(eid, "the corrected text")
    assert entry is not None
    assert entry["accepted"] is True
    assert entry["correction"] == "the corrected text"
    assert entry["supersedes"] is not None


def test_record_acceptance_preserves_corpus_for_instance_scope(
    tmp_errorta_home: Path,
) -> None:
    eid = metrics.record_verdict(
        "p",
        "a",
        {"rating": "fail"},
        None,
        prompt_signature="a" * 64,
        corpus="welcome",
    )
    entry = metrics.record_acceptance(eid, "the corrected text")

    assert entry is not None
    assert entry["corpus"] == "welcome"
    assert entry["prompt_signature"] == "a" * 64


def test_record_acceptance_unknown_id_returns_none(tmp_errorta_home: Path) -> None:
    assert metrics.record_acceptance("does-not-exist", "x") is None


def test_find_accepted_correction_last_write_wins(tmp_errorta_home: Path) -> None:
    eid = metrics.record_verdict("p", "a", {"rating": "fail"}, None)
    metrics.record_acceptance(eid, "first correction")
    eid2 = metrics.record_verdict("p", "a2", {"rating": "fail"}, None)
    metrics.record_acceptance(eid2, "second correction")
    assert metrics.find_accepted_correction("p") == "second correction"
    assert metrics.find_accepted_correction("nonexistent") is None


def test_summary_pass_rate_and_trend_bucketing(
    tmp_errorta_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seed entries directly so we control created_at timestamps.
    fixed_now = _dt.datetime(2026, 6, 7, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def _seed(rating: str, created: _dt.datetime, accepted: bool = False,
              correction: str | None = None, prompt: str = "p") -> None:
        import json
        import uuid
        entry = {
            "id": uuid.uuid4().hex,
            "prompt": prompt,
            "answer": "a",
            "verdict": {"rating": rating, "failure_tags": []},
            "judge_model": None,
            "accepted": accepted,
            "correction": correction,
            "created_at": created.isoformat(),
        }
        with metrics.log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # 3 pass, 1 fail in last 7d; 1 fail older than 7d.
    _seed("pass", fixed_now - _dt.timedelta(days=1))
    _seed("pass", fixed_now - _dt.timedelta(days=2))
    _seed("pass", fixed_now - _dt.timedelta(days=2))
    _seed("fail", fixed_now - _dt.timedelta(days=3), accepted=True,
          correction="fix-A", prompt="recurring")
    _seed("fail", fixed_now - _dt.timedelta(days=30))
    # Two more acceptances on the recurring prompt to test most_corrected.
    _seed("fail", fixed_now - _dt.timedelta(days=4), accepted=True,
          correction="fix-B", prompt="recurring")
    _seed("fail", fixed_now - _dt.timedelta(days=5), accepted=True,
          correction="fix-C", prompt="other")

    s = metrics.summary(now=fixed_now)
    assert s["total"] == 7
    assert s["total_7d"] == 6
    # 3 pass / 7 total => 0.4286
    assert s["pass_rate"] == pytest.approx(0.4286, abs=1e-4)
    # 3 pass / 6 in 7d
    assert s["pass_rate_7d"] == pytest.approx(0.5)
    # trend has exactly 7 buckets
    assert len(s["trend_7d"]) == 7
    # most-corrected dedupes prompts
    by_prompt = {row["prompt"]: row["count"] for row in s["most_corrected_prompts"]}
    assert by_prompt.get("recurring") == 2
    assert by_prompt.get("other") == 1


def test_summary_empty_log(tmp_errorta_home: Path) -> None:
    s = metrics.summary()
    assert s["total"] == 0
    assert s["pass_rate"] is None
    assert s["total_7d"] == 0
    assert s["pass_rate_7d"] is None
    assert len(s["trend_7d"]) == 7


def test_record_verdict_persists_prompt_signature(tmp_errorta_home: Path) -> None:
    """F001-deepen-01: keyword-only prompt_signature lands in the log entry."""
    import json as _json

    eid = metrics.record_verdict(
        prompt="what orbits earth?",
        answer="the moon",
        verdict={"rating": "pass", "failure_tags": []},
        judge_model=None,
        prompt_signature="a" * 64,
    )
    assert eid
    lines = [
        line for line in metrics.log_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1
    entry = _json.loads(lines[0])
    assert entry["prompt_signature"] == "a" * 64


def _seed_latency(tmp_errorta_home: Path, latencies: list[float | None]) -> None:
    import json
    import uuid

    for v in latencies:
        verdict: dict = {"rating": "pass", "failure_tags": []}
        if v is not None:
            verdict["latency_ms"] = v
        entry = {
            "id": uuid.uuid4().hex,
            "prompt": "p",
            "answer": "a",
            "verdict": verdict,
            "judge_model": None,
            "accepted": False,
            "correction": None,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
        with metrics.log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def test_latency_histogram_empty(tmp_errorta_home: Path) -> None:
    s = metrics.summary()
    hist = s["latency_histogram"]
    assert [b["label"] for b in hist["buckets"]] == [
        "0-250", "250-500", "500-750", "750-1000", "1000-2000", "2000+",
    ]
    assert all(b["count"] == 0 for b in hist["buckets"])
    assert hist["p50_ms"] is None
    assert hist["p95_ms"] is None
    assert hist["p99_ms"] is None


def test_latency_histogram_single_sample(tmp_errorta_home: Path) -> None:
    _seed_latency(tmp_errorta_home, [123.0])
    hist = metrics.summary()["latency_histogram"]
    counts = {b["label"]: b["count"] for b in hist["buckets"]}
    assert counts["0-250"] == 1
    assert sum(counts.values()) == 1
    # Nearest-rank with n=1 always picks the single sample.
    assert hist["p50_ms"] == 123.0
    assert hist["p95_ms"] == 123.0
    assert hist["p99_ms"] == 123.0


def test_latency_histogram_mixed_distribution(tmp_errorta_home: Path) -> None:
    # Place one sample in each bucket including the unbounded 2000+ bucket.
    _seed_latency(tmp_errorta_home, [10.0, 300.0, 600.0, 900.0, 1500.0, 3000.0])
    hist = metrics.summary()["latency_histogram"]
    counts = {b["label"]: b["count"] for b in hist["buckets"]}
    assert counts == {
        "0-250": 1,
        "250-500": 1,
        "500-750": 1,
        "750-1000": 1,
        "1000-2000": 1,
        "2000+": 1,
    }
    assert [b["label"] for b in hist["buckets"]] == [
        "0-250", "250-500", "500-750", "750-1000", "1000-2000", "2000+",
    ]


def test_latency_histogram_skips_missing(tmp_errorta_home: Path) -> None:
    _seed_latency(tmp_errorta_home, [None, 100.0, None, 200.0])
    hist = metrics.summary()["latency_histogram"]
    counts = {b["label"]: b["count"] for b in hist["buckets"]}
    assert counts["0-250"] == 2
    assert sum(counts.values()) == 2
    assert hist["p50_ms"] is not None


def test_latency_histogram_bucket_boundaries(tmp_errorta_home: Path) -> None:
    # Boundary values must go into the upper bucket (lower_inclusive).
    _seed_latency(tmp_errorta_home, [250.0, 500.0, 750.0, 1000.0, 2000.0])
    hist = metrics.summary()["latency_histogram"]
    counts = {b["label"]: b["count"] for b in hist["buckets"]}
    assert counts == {
        "0-250": 0,
        "250-500": 1,
        "500-750": 1,
        "750-1000": 1,
        "1000-2000": 1,
        "2000+": 1,
    }


def test_percentile_nearest_rank_known_list(tmp_errorta_home: Path) -> None:
    # Known list: [10, 20, 30, ..., 100], n=10.
    # Nearest rank: p50 -> ceil(0.5*10)=5 -> values[4]=50
    #               p95 -> ceil(0.95*10)=10 -> values[9]=100
    #               p99 -> ceil(0.99*10)=10 -> values[9]=100
    _seed_latency(tmp_errorta_home, [float(v) for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]])
    hist = metrics.summary()["latency_histogram"]
    assert hist["p50_ms"] == 50.0
    assert hist["p95_ms"] == 100.0
    assert hist["p99_ms"] == 100.0


def test_concurrent_record_verdict_thread_safety(tmp_errorta_home: Path) -> None:
    """Five concurrent writers must all land — no torn lines, all ids present."""

    def writer(i: int) -> None:
        metrics.record_verdict(f"prompt-{i}", "ans", {"rating": "pass"}, None)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [
        line for line in metrics.log_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 5
    import json
    parsed = [json.loads(line) for line in lines]
    assert len({p["id"] for p in parsed}) == 5
