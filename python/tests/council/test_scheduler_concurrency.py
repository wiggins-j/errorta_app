"""Invariant 2: exactly one writer task per run (Fix 4 — RunWriterToken).

This test EXERCISES the token enforcement that makes invariant 2 testable:
  (a) external append_event WITHOUT a token raises NotAuthorizedWriter;
  (b) external append_event WITH an invalid/wrong/released token raises
      NotAuthorizedWriter;
  (c) the registered writer task (held via acquire_writer) succeeds and
      its 8 sequential append_event calls receive strictly monotonic
      sequences.
"""
from __future__ import annotations

import asyncio

import pytest

from errorta_council.run_store import (
    NotAuthorizedWriter,
    RunStore,
    RunWriterToken,
)
from errorta_council.schema import EventStatus, EventType


@pytest.mark.asyncio
async def test_eight_concurrent_external_no_token_writers_rejected(
    tmp_errorta_home, runs_dir_path
) -> None:
    """Invariant 2 (a): without a token, external writers are rejected."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"id": "rm", "members": []}, prompt="hi", corpus_ids=[]
    )

    async def _writer(i: int) -> Exception | None:
        try:
            await asyncio.to_thread(
                store.append_event,
                meta.id,
                type=EventType.DIAGNOSTIC_NOTE,
                status=EventStatus.COMPLETED,
                payload={"writer": i},
            )
            return None
        except NotAuthorizedWriter as exc:
            return exc

    results = await asyncio.gather(*[_writer(i) for i in range(8)])
    assert all(isinstance(r, NotAuthorizedWriter) for r in results)
    _, events = store.read_run(meta.id)
    assert not any(e.type == EventType.DIAGNOSTIC_NOTE for e in events)


@pytest.mark.asyncio
async def test_eight_concurrent_external_invalid_token_writers_rejected(
    tmp_errorta_home, runs_dir_path
) -> None:
    """Invariant 2 (b): with an invalid token, external writers are rejected."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"id": "rm", "members": []}, prompt="hi", corpus_ids=[]
    )
    bogus = RunWriterToken(run_id=meta.id, token="invalid-token-value")

    async def _writer(i: int) -> Exception | None:
        try:
            await asyncio.to_thread(
                store.append_event,
                meta.id,
                type=EventType.DIAGNOSTIC_NOTE,
                status=EventStatus.COMPLETED,
                payload={"writer": i},
                writer=bogus,
            )
            return None
        except NotAuthorizedWriter as exc:
            return exc

    results = await asyncio.gather(*[_writer(i) for i in range(8)])
    assert all(isinstance(r, NotAuthorizedWriter) for r in results)


@pytest.mark.asyncio
async def test_registered_writer_eight_sequential_appends_monotonic(
    tmp_errorta_home, runs_dir_path
) -> None:
    """Invariant 2 (c): the registered writer's appends serialize on per-run lock."""
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm", room_snapshot={"id": "rm", "members": []}, prompt="hi", corpus_ids=[]
    )
    token = store.acquire_writer(meta.id)
    try:
        for i in range(8):
            await asyncio.to_thread(
                store.append_event,
                meta.id,
                type=EventType.DIAGNOSTIC_NOTE,
                status=EventStatus.COMPLETED,
                payload={"i": i},
                writer=token,
            )
    finally:
        store.release_writer(token)

    _, events = store.read_run(meta.id)
    diag_events = [e for e in events if e.type == EventType.DIAGNOSTIC_NOTE]
    assert len(diag_events) == 8
    seqs = [e.sequence for e in diag_events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 8


@pytest.mark.asyncio
async def test_two_runs_proceed_independently(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta_a = store.create_run(
        room_id="rm-a", room_snapshot={"id": "rm-a", "members": []}, prompt="a", corpus_ids=[]
    )
    meta_b = store.create_run(
        room_id="rm-b", room_snapshot={"id": "rm-b", "members": []}, prompt="b", corpus_ids=[]
    )

    async def _interleave(run_id: str, tag: str) -> None:
        token = store.acquire_writer(run_id)
        try:
            for i in range(4):
                await asyncio.to_thread(
                    store.append_event,
                    run_id,
                    type=EventType.DIAGNOSTIC_NOTE,
                    status=EventStatus.COMPLETED,
                    payload={"tag": tag, "i": i},
                    writer=token,
                )
        finally:
            store.release_writer(token)

    await asyncio.gather(
        _interleave(meta_a.id, "a"),
        _interleave(meta_b.id, "b"),
    )
    _, events_a = store.read_run(meta_a.id)
    _, events_b = store.read_run(meta_b.id)
    a_seqs = [e.sequence for e in events_a if e.type == EventType.DIAGNOSTIC_NOTE]
    b_seqs = [e.sequence for e in events_b if e.type == EventType.DIAGNOSTIC_NOTE]
    assert a_seqs == sorted(a_seqs) and len(set(a_seqs)) == 4
    assert b_seqs == sorted(b_seqs) and len(set(b_seqs)) == 4
