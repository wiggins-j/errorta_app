"""Invariant 2 (writer ownership): exactly one task may write a given run's events.

Enforced — not by convention — via RunWriterToken. The scheduler acquires
the token at run start; only it may then call append_event with the live
token. External callers without a token (or with a wrong/expired token)
are rejected with NotAuthorizedWriter.
"""
from __future__ import annotations

import pytest

from errorta_council.run_store import (
    NotAuthorizedWriter,
    RunStore,
    RunWriterToken,
    WriterAlreadyHeld,
)
from errorta_council.schema import EventStatus, EventType


def test_acquire_writer_returns_token_for_run(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    token = store.acquire_writer(meta.id)
    assert isinstance(token, RunWriterToken)
    assert token.run_id == meta.id
    assert isinstance(token.token, str) and len(token.token) >= 16


def test_acquire_writer_twice_raises_writer_already_held(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    store.acquire_writer(meta.id)
    with pytest.raises(WriterAlreadyHeld):
        store.acquire_writer(meta.id)


def test_release_writer_allows_reacquire(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    token = store.acquire_writer(meta.id)
    store.release_writer(token)
    again = store.acquire_writer(meta.id)
    assert again.token != token.token


def test_append_event_without_token_raises(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    with pytest.raises(NotAuthorizedWriter):
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
        )


def test_append_event_with_invalid_token_raises(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    bogus = RunWriterToken(run_id=meta.id, token="not-a-real-token")
    with pytest.raises(NotAuthorizedWriter):
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
            writer=bogus,
        )


def test_append_event_with_wrong_run_id_token_raises(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    a = store.create_run(room_id="rm-a", room_snapshot={}, prompt="hi", corpus_ids=[])
    b = store.create_run(room_id="rm-b", room_snapshot={}, prompt="hi", corpus_ids=[])
    token_b = store.acquire_writer(b.id)
    with pytest.raises(NotAuthorizedWriter):
        store.append_event(
            a.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
            writer=token_b,
        )


def test_released_token_is_rejected(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    token = store.acquire_writer(meta.id)
    store.release_writer(token)
    with pytest.raises(NotAuthorizedWriter):
        store.append_event(
            meta.id,
            type=EventType.RUN_STARTED,
            status=EventStatus.RUNNING,
            payload={},
            writer=token,
        )


def test_append_event_with_valid_token_succeeds(
    tmp_errorta_home, runs_dir_path
) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm", room_snapshot={}, prompt="hi", corpus_ids=[])
    token = store.acquire_writer(meta.id)
    ev = store.append_event(
        meta.id,
        type=EventType.RUN_STARTED,
        status=EventStatus.RUNNING,
        payload={},
        writer=token,
    )
    assert ev.sequence == 1
