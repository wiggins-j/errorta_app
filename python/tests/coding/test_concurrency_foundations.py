"""F087 Slice 0 — concurrency foundations.

Locks down the three things that made parallel dispatch unsafe:
1. the shared event-loop bridge (the old asyncio.run-per-thread path deadlocked
   on the providers' shared asyncio.Semaphore);
2. serialized ledger writers (no lost records under concurrent worker turns);
3. strict max_model_calls budget reservation before dispatch.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from errorta_council.coding.autonomy import (
    CodingAutonomyPolicy,
    LoopCounters,
    reserve_model_calls,
)
from errorta_council.coding.ledger import LedgerStore
from errorta_model_gateway import loop_bridge
from errorta_model_gateway.providers import async_claude_cli, async_codex_cli, async_cursor_cli

# --- shared event-loop bridge ----------------------------------------------


def test_run_coro_no_deadlock_under_many_threads() -> None:
    """Regression: many threads each running a coroutine via the shared loop all
    complete (the old asyncio.run-per-thread path hung on a shared semaphore)."""
    async def work(x: int) -> int:
        await asyncio.sleep(0.01)
        return x * 2

    results: dict[int, int] = {}
    errors: list[Exception] = []

    def call(i: int) -> None:
        try:
            results[i] = loop_bridge.run_coro(work(i), timeout=15)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads), "run_coro deadlocked under threads"
    assert not errors, errors
    assert results == {i: i * 2 for i in range(12)}


def test_shared_loop_semaphore_actually_bounds_concurrency() -> None:
    """An asyncio.Semaphore now binds to the ONE shared loop, so it bounds
    concurrency instead of deadlocking across per-thread loops."""
    sem = asyncio.Semaphore(2)
    state = {"cur": 0, "peak": 0}
    guard = threading.Lock()

    async def guarded() -> bool:
        async with sem:
            with guard:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            await asyncio.sleep(0.05)
            with guard:
                state["cur"] -= 1
        return True

    threads = [
        threading.Thread(target=lambda: loop_bridge.run_coro(guarded(), timeout=20))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)
    assert not any(t.is_alive() for t in threads)
    assert state["peak"] <= 2  # the gate held on the shared loop


# --- configurable CLI concurrency cap --------------------------------------


def test_cli_concurrency_is_configurable() -> None:
    try:
        async_claude_cli.set_claude_concurrency(5)
        async_codex_cli.set_codex_concurrency(3)
        async_cursor_cli.set_cursor_concurrency(4)
        assert async_claude_cli._CLAUDE_SEMAPHORE._value == 5
        assert async_codex_cli._CODEX_SEMAPHORE._value == 3
        assert async_cursor_cli._CURSOR_SEMAPHORE._value == 4
        async_claude_cli.set_claude_concurrency(0)  # clamps to >= 1
        async_cursor_cli.set_cursor_concurrency(0)
        assert async_claude_cli._CLAUDE_SEMAPHORE._value == 1
        assert async_cursor_cli._CURSOR_SEMAPHORE._value == 1
    finally:  # don't leak a resized gate into other tests
        async_claude_cli.set_claude_concurrency(2)
        async_codex_cli.set_codex_concurrency(2)
        async_cursor_cli.set_cursor_concurrency(2)


# --- serialized ledger writers ---------------------------------------------


class _FakeSession:
    command_ids = ["unit"]
    unknown_ids: list[str] = []
    passed = True
    results: list = []
    sandbox = "none"


def test_concurrent_ledger_writers_lose_no_records(tmp_path: Path) -> None:
    s = LedgerStore("conc", root=tmp_path)
    s.create_project(north_star="n", definition_of_done="d", target="new", repo_path=None)
    workers, per = 6, 15

    def hammer(w: int) -> None:
        for i in range(per):
            s.record_decision(title=f"d{w}-{i}", context="c", choice="x", rationale="r")
            s.record_turn(role="dev", member_id=f"m{w}", task_id="t",
                          prompt="p", response="r", outcome="ok")
            s.record_episode(title=f"e{w}-{i}", summary="s")
            s.record_test_run(_FakeSession(), task_id="t", head="h")
            s.record_tool_event(
                turn_id=f"turn-{w}-{i}",
                task_id="t",
                member_id=f"m{w}",
                role="dev",
                tool="code_write",
                status="ok",
            )
            s.upsert_artifact(path=f"f{w}-{i}.py", status="written",
                              last_task_id="t", content_sha256="h")

    threads = [threading.Thread(target=hammer, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    n = workers * per
    assert len(s.list_decisions()) == n
    assert len(s.list_turns()) == n
    assert len(s.list_episodes()) == n
    assert len(s.list_test_runs()) == n
    assert len(s.list_tool_events()) == n
    assert len(s.list_artifacts()) == n  # distinct paths -> none lost in r-m-w


# --- strict budget reservation ---------------------------------------------


def test_reserve_model_calls_strict() -> None:
    unlimited = CodingAutonomyPolicy(max_model_calls=None)
    assert reserve_model_calls(LoopCounters(model_calls=99), unlimited, 4) == 4

    capped = CodingAutonomyPolicy(max_model_calls=10)
    assert reserve_model_calls(LoopCounters(model_calls=0), capped, 4) == 4   # fits
    assert reserve_model_calls(LoopCounters(model_calls=7), capped, 5) == 3   # shrink
    assert reserve_model_calls(LoopCounters(model_calls=10), capped, 5) == 0  # exhausted
    assert reserve_model_calls(LoopCounters(model_calls=0), capped, 0) == 0   # nothing
