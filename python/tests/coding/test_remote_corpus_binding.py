"""F088 enablement Slice 1 — remote-aware corpus binding foundation.

A corpus that lives on a remote AIAR (watchdog) must derive its health from the
remote instance, not the local manifest — otherwise a healthy remote corpus is
falsely 'missing'. save_binding must preserve remote health instead of forcing
the local probe.
"""
from __future__ import annotations

from pathlib import Path

from errorta_council.coding.ledger import LedgerStore
from errorta_project_grounding.corpus_binding import (
    ProjectCorpusBinding,
    binding_status,
    load_binding,
    save_binding,
)


class _FakeRemote:
    """Stand-in adapter exposing instance_health(corpus_id)."""
    def __init__(self, *, health=None, raises=None):
        self._health = health or {}
        self._raises = raises

    def instance_health(self, corpus_id):
        if self._raises:
            raise self._raises
        return self._health


def _store(tmp: Path, pid: str) -> LedgerStore:
    s = LedgerStore(pid, root=tmp)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    return s


def _remote(pid, **kw) -> ProjectCorpusBinding:
    base = dict(project_id=pid, mode="build_from_repo", corpus_id="proj-corpus",
                source_root="/tmp/src", adapter_source="remote")
    base.update(kw)
    return ProjectCorpusBinding(**base)


# --- the marker round-trips -------------------------------------------------


def test_adapter_source_round_trips(tmp_path: Path) -> None:
    s = _store(tmp_path, "rt1")
    saved = save_binding(s, _remote("rt1", health_state="indexing", health_reason="bootstrap running"))
    assert saved.is_remote
    reloaded = load_binding(s)
    assert reloaded.adapter_source == "remote" and reloaded.is_remote
    assert ProjectCorpusBinding.from_dict(saved.to_dict()).adapter_source == "remote"


# --- save_binding does NOT force the local manifest probe for remote --------


def test_save_remote_preserves_health_not_local_missing(tmp_path: Path) -> None:
    s = _store(tmp_path, "rt2")
    # no local corpus manifest exists; a LOCAL binding would be marked 'missing'.
    saved = save_binding(s, _remote("rt2", health_state="indexing", health_reason="bootstrap running"))
    assert saved.health_state == "indexing"  # preserved, not downgraded to missing


def test_save_local_still_uses_manifest_probe(tmp_path: Path) -> None:
    s = _store(tmp_path, "rt3")
    # a local build_from_repo binding with no manifest -> missing (unchanged behavior)
    b = ProjectCorpusBinding(project_id="rt3", mode="build_from_repo",
                             corpus_id="local-corpus", source_root="/tmp/src")
    saved = save_binding(s, b)
    assert saved.health_state == "missing"


# --- binding_status derives remote health from instance_health --------------


def test_remote_status_ready_when_published_with_content(tmp_path: Path) -> None:
    b = _remote("rt4", health_state="missing")
    out = binding_status(b, adapter=_FakeRemote(health={"published": True, "chunk_count": 42}))
    assert out.health_state == "ready" and "42" in out.health_reason


def test_remote_status_missing_when_instance_not_found(tmp_path: Path) -> None:
    # A 404 / unknown_instance is the normal "not built yet" case — surface a
    # clean message, not the raw HTTP error.
    b = _remote("rt5", health_state="ready")
    out = binding_status(b, adapter=_FakeRemote(raises=RuntimeError("404 unknown instance")))
    assert out.health_state == "missing" and "not found" in out.health_reason
    assert "lookup failed" not in out.health_reason


def test_remote_status_404_for_build_from_project_is_actionable(tmp_path: Path) -> None:
    from dataclasses import replace
    b = replace(_remote("rt5b", health_state="ready"), mode="build_from_project")
    out = binding_status(b, adapter=_FakeRemote(raises=RuntimeError("404 unknown_instance")))
    assert out.health_state == "missing"
    assert "Build a corpus from this project" in out.health_reason


def test_remote_status_keeps_raw_error_for_non_404(tmp_path: Path) -> None:
    # A genuine transport failure (not a 404) still surfaces the underlying error.
    b = _remote("rt5c", health_state="ready")
    out = binding_status(b, adapter=_FakeRemote(raises=RuntimeError("connection refused")))
    assert out.health_state == "missing" and "lookup failed" in out.health_reason


def test_remote_status_missing_when_empty(tmp_path: Path) -> None:
    b = _remote("rt6", health_state="ready")
    out = binding_status(b, adapter=_FakeRemote(health={"published": False, "chunk_count": 0}))
    assert out.health_state == "missing"


def test_remote_status_requires_explicit_published_true(tmp_path: Path) -> None:
    b = _remote("rt6b", health_state="ready")
    missing_flag = binding_status(b, adapter=_FakeRemote(health={"chunk_count": 5}))
    assert missing_flag.health_state == "missing"

    unpublished = binding_status(b, adapter=_FakeRemote(health={"published": False, "chunk_count": 5}))
    assert unpublished.health_state == "missing"


def test_remote_status_preserves_indexing_and_failed(tmp_path: Path) -> None:
    # the bootstrap owns these states; instance_health must not override them
    idx = binding_status(_remote("rt7", health_state="indexing"),
                         adapter=_FakeRemote(health={"published": True, "chunk_count": 5}))
    assert idx.health_state == "indexing"
    fail = binding_status(_remote("rt8", health_state="failed", health_reason="ingest error"),
                          adapter=_FakeRemote(health={"published": True, "chunk_count": 5}))
    assert fail.health_state == "failed"


def test_remote_status_preserved_when_no_remote_adapter(tmp_path: Path) -> None:
    # adapter without instance_health (e.g. local adapter in this process) ->
    # trust the stored state rather than falsely downgrading.
    class _Local:  # no instance_health
        pass
    b = _remote("rt9", health_state="ready", health_reason="remote instance ready (9 chunks)")
    out = binding_status(b, adapter=_Local())
    assert out.health_state == "ready"
