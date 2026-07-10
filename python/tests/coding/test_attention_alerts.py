"""F117-04 — reviewer Alerts producer."""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding import attention
from errorta_council.coding.ledger import LedgerStore


def _store(tmp_path: Path) -> LedgerStore:
    return LedgerStore("alerts", root=tmp_path)


def test_raise_review_alert_is_advisory_and_nonblocking(tmp_path: Path):
    s = _store(tmp_path)
    sig = attention.raise_review_alert(
        "alerts", stage="reviewing_build", title="button vs autosave",
        summary="No guidance on save UX", store=s)
    assert sig is not None
    assert sig.kind == "alert" and sig.blocking is False and sig.source == "reviewer"
    # an alert never blocks the stage
    assert attention.blocks_stage("alerts", "reviewing_build", store=s) is False


def test_review_alert_dedups_by_stage_title(tmp_path: Path):
    s = _store(tmp_path)
    a = attention.raise_review_alert(
        "alerts", stage="reviewing_build", title="dup", summary="x", store=s)
    assert a is not None
    # same stage+title while open → deduped
    assert attention.raise_review_alert(
        "alerts", stage="reviewing_build", title="dup", summary="y", store=s) is None
    # a different title is not deduped
    assert attention.raise_review_alert(
        "alerts", stage="reviewing_build", title="other", summary="z", store=s) is not None


def test_review_alert_four_actions_resolve(tmp_path: Path):
    s = _store(tmp_path)
    a = attention.raise_review_alert(
        "alerts", stage="reviewing_build", title="a", summary="b", store=s)
    # an Alert accepts the advisory four-action set
    upd, task = attention.resolve("alerts", a.id, "defer", store=s)
    assert upd.state == "deferred" and task is None
