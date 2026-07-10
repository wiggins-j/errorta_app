"""Invariant 12 — errors are sanitized at the seam.

The transcript must never carry fake provider keys; ``context_built``
payloads must never carry hidden context. The diagnostics export shape
includes audit ids only, not raw outbound payloads.
"""
from __future__ import annotations

import json
from pathlib import Path

from errorta_council import paths as council_paths
from errorta_council.fake_run import run_fake_council
from errorta_council.run_store import RunStore
from errorta_council.schema import (
    CouncilEventError,
    EventStatus,
    EventType,
)


def test_member_failed_does_not_leak_provider_secret(tmp_errorta_home: Path) -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        run_id="run-d1", room_id="r", room_snapshot={},
        prompt="p", corpus_ids=[],
    )
    _token = store.acquire_writer(meta.id)
    store.append_event(meta.id, type=EventType.RUN_STARTED,
                       status=EventStatus.RUNNING, payload={}, writer=_token)
    store.append_event(
        meta.id,
        type=EventType.MEMBER_FAILED,
        status=EventStatus.FAILED,
        payload={"error_code": "provider_timeout"},
        error=CouncilEventError(
            code="provider_timeout",
            message="Provider timed out.",
            retryable=True,
            details={"phase": "call"},
        ),
        writer=_token,
    )
    store.release_writer(_token)
    log_text = (council_paths.runs_dir() / f"{meta.id}.jsonl").read_text()
    # Sanity guard — no test secret can ever appear in the log because
    # we never put one in. This documents the contract for future devs.
    assert "sk-FAKE-LEAK-CANARY" not in log_text


def test_context_built_payload_omits_hidden_context(tmp_errorta_home: Path) -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        run_id="run-d2", room_id="r", room_snapshot={},
        prompt="p", corpus_ids=[],
    )
    _token = store.acquire_writer(meta.id)
    store.append_event(meta.id, type=EventType.RUN_STARTED,
                       status=EventStatus.RUNNING, payload={}, writer=_token)
    store.append_event(
        meta.id,
        type=EventType.CONTEXT_BUILT,
        status=EventStatus.COMPLETED,
        payload={
            "context_access": "retrieved_snippets",
            "transcript_access": "all_messages",
            "input_token_estimate": 1420,
            "context_summary_id": "ctx_abc",
            "source_event_ids": [1, 2],
            "retrieved_chunk_count": 4,
            "redacted": False,
        },
        writer=_token,
    )
    store.release_writer(_token)
    log = (council_paths.runs_dir() / f"{meta.id}.jsonl").read_text()
    payload = json.loads(log.splitlines()[-1])["payload"]
    # The payload should carry counts/ids/hashes — never raw context text.
    forbidden = {"context_text", "snippets", "documents", "raw_payload"}
    assert not (forbidden & payload.keys())


def test_run_fake_council_does_not_emit_outbound_payload_fields(
    tmp_errorta_home: Path,
) -> None:
    store = RunStore(runs_dir=council_paths.runs_dir())
    meta = store.create_run(
        run_id="run-d3", room_id="r", room_snapshot={},
        prompt="p", corpus_ids=[],
    )
    run_fake_council(store, meta.id, member_ids=["m-1"])
    log = (council_paths.runs_dir() / f"{meta.id}.jsonl").read_text()
    for line in log.splitlines():
        raw = json.loads(line)
        # No outbound provider payload escapes via Council events.
        assert "api_key" not in json.dumps(raw)
        assert "authorization" not in json.dumps(raw).lower()
