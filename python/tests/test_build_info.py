"""Build provenance — the backbone of the stale-build check."""
from __future__ import annotations

import json

from errorta_app import build_info as bi


def _fresh():
    bi.build_info.cache_clear()


def test_git_fallback_from_source(monkeypatch) -> None:
    # running from a source checkout -> reports the live HEAD
    monkeypatch.delenv("ERRORTA_BUILD_COMMIT", raising=False)
    monkeypatch.setattr(bi, "_from_bundle", lambda: None)
    _fresh()
    info = bi.build_info()
    assert info["source"] in ("git", "unknown")
    if info["source"] == "git":
        assert info["commit"] and len(info["commit"]) >= 7
        assert info["commit_short"] == info["commit"][:12]
    _fresh()


def test_env_overrides_git(monkeypatch) -> None:
    monkeypatch.setattr(bi, "_from_bundle", lambda: None)
    monkeypatch.setenv("ERRORTA_BUILD_COMMIT", "abc123def456789")
    monkeypatch.setenv("ERRORTA_BUILT_AT", "2026-06-17T00:00:00Z")
    monkeypatch.setenv("ERRORTA_BUILD_DIRTY", "1")
    _fresh()
    info = bi.build_info()
    assert info["source"] == "env" and info["commit"] == "abc123def456789"
    assert info["commit_short"] == "abc123def456" and info["dirty"] is True
    assert info["built_at"] == "2026-06-17T00:00:00Z"
    _fresh()


def test_bundle_wins_over_env(monkeypatch, tmp_path) -> None:
    blob = {"commit": "bundledcommit0001", "built_at": "2026-06-16T21:02:00Z",
            "dirty": False, "source": "bundled"}
    monkeypatch.setattr(bi, "_from_bundle", lambda: dict(blob))
    monkeypatch.setenv("ERRORTA_BUILD_COMMIT", "envwins-not")
    _fresh()
    info = bi.build_info()
    assert info["commit"] == "bundledcommit0001" and info["source"] == "bundled"
    _fresh()


def test_never_raises_when_nothing_available(monkeypatch) -> None:
    monkeypatch.setattr(bi, "_from_bundle", lambda: None)
    monkeypatch.setattr(bi, "_from_env", lambda: None)
    monkeypatch.setattr(bi, "_from_git", lambda: None)
    _fresh()
    info = bi.build_info()
    assert info["source"] == "unknown" and info["commit"] is None
    assert info["commit_short"] is None
    _fresh()


def test_features_reports_grounding() -> None:
    f = bi.features()
    assert f["coding"] is True and f["council"] is True
    # this checkout has errorta_project_grounding -> grounding supported
    assert f["grounding"] is True


def test_bundle_reader_parses_real_file(tmp_path, monkeypatch) -> None:
    p = tmp_path / "_build_info.json"
    p.write_text(json.dumps({"commit": "deadbeef"}), encoding="utf-8")
    monkeypatch.setattr(bi, "_bundled_paths", lambda: [p])
    assert bi._from_bundle()["commit"] == "deadbeef"
    # a malformed / commit-less file is ignored, not crashed on
    p.write_text("{ not json", encoding="utf-8")
    assert bi._from_bundle() is None
