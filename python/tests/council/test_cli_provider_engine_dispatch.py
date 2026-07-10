"""F040 — a subscription-CLI member dispatches through the full council engine.

The handler unit tests prove the CLI adapter in isolation. This proves the
*wiring*: build_and_run with a `claude_cli` member routes through the real
LocalGateway → F034 registry → ClaudeCliHandler (mocked subprocess), the member
is classified `remote` egress (so verify_payload_route_alignment passes), and a
MEMBER_MESSAGE lands with the CLI's answer. No real CLI needed.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from errorta_council.engine import build_and_run
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


@pytest.fixture(autouse=True)
def _stub_cli_binaries(monkeypatch):
    """Make the CLI binary probes machine-independent.

    These tests mock the CLI *subprocess*, but the handlers now resolve the
    real binary (PATH / known locations) before spawning — so on a machine
    without ``claude``/``codex`` installed the handler short-circuits with
    ``*_not_installed`` BEFORE the mocked subprocess runs, and the test fails
    only by environment. Pin the resolvers to a stub path so the dispatch
    path is exercised everywhere (CI included), not just on a dev box that
    happens to have the CLIs.
    """
    import errorta_model_gateway.providers.async_claude_cli as claude_mod
    import errorta_model_gateway.providers.async_codex_cli as codex_mod
    import errorta_model_gateway.providers.async_cursor_cli as cursor_mod

    monkeypatch.setattr(claude_mod, "resolve_claude_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(codex_mod, "resolve_codex_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(
        cursor_mod,
        "resolve_cursor_command",
        lambda: cursor_mod.CursorCommand(["/usr/bin/true"], "/usr/bin/true"),
    )


class _FakeProc:
    def __init__(self, stdout: bytes):
        self._stdout = stdout
        self.returncode = 0

    async def communicate(self, input=None):
        return self._stdout, b""

    def terminate(self): ...
    def kill(self): ...
    async def wait(self): return 0


class _FakeMeta:
    async def is_reachable(self) -> bool:
        return True

    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


def _claude_json(text: str) -> bytes:
    return json.dumps({
        "type": "result", "is_error": False, "result": text,
        "usage": {"input_tokens": 100, "output_tokens": 7},
    }).encode("utf-8")


@pytest.mark.asyncio
async def test_claude_cli_member_runs_through_engine(
    tmp_errorta_home, runs_dir_path, monkeypatch
):
    # Mock the shared CLI runner's subprocess so no real `claude` is spawned.
    import errorta_model_gateway.providers._cli_common as common

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(_claude_json("Paris is the capital of France."))

    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)

    room = {
        "id": "rm-cli",
        "context_access_ceiling": "full_context",
        "transcript_access_ceiling": "all_messages",
        "allow_full_context": True,
        # Permit remote egress (the CLI phones home → remote).
        "context_policy": {
            "require_confirmation_for_remote_context": False,
            "require_confirmation_for_full_context": False,
        },
        "corpus_policy": {"max_egress_class": "remote_eligible"},
        "residency": {"destination_scope": "remote"},
        "members": [
            {
                "id": "m-claude", "enabled": True, "role": "member",
                "provider": "claude_cli", "model": "haiku",
                "gateway_route_id": "claude_cli.haiku",
                "context_access": "prompt_only",
                "transcript_access": "none",
            },
        ],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "transcript_only", "finalizer_member_id": None},
    }

    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-cli", room_snapshot=room,
                            prompt="What is the capital of France?", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=10),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
            # No gateway override → real LocalGateway → registry → claude_cli.
        ),
        timeout=15.0,
    )
    assert final.status == "completed"
    msgs = [e for e in store.read_run(meta.id)[1] if e.type == EventType.MEMBER_MESSAGE]
    assert len(msgs) == 1
    assert msgs[0].payload["content"] == "Paris is the capital of France."
    # The final answer surfaces the CLI member's message.
    fa = [e for e in store.read_run(meta.id)[1] if e.type == EventType.FINAL_ANSWER]
    assert fa and fa[-1].payload["content"] == "Paris is the capital of France."


@pytest.mark.asyncio
async def test_cursor_cli_member_runs_through_engine(
    tmp_errorta_home, runs_dir_path, monkeypatch
):
    import errorta_model_gateway.providers._cli_common as common

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(_claude_json("Cursor can take this coding role."))

    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)

    room = {
        "id": "rm-cursor-cli",
        "allow_full_context": True,
        "context_policy": {
            "require_confirmation_for_remote_context": False,
            "require_confirmation_for_full_context": False,
        },
        "corpus_policy": {"max_egress_class": "remote_eligible"},
        "residency": {"destination_scope": "remote"},
        "members": [
            {
                "id": "Cursor", "enabled": True, "role": "member",
                "provider": "cursor_cli", "model": "gpt-5",
                "gateway_route_id": "cursor_cli.gpt-5",
                "context_access": "prompt_only",
                "transcript_access": "none",
                "metadata": {"coding_role": "reviewer"},
            },
        ],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "transcript_only", "finalizer_member_id": None},
    }

    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-cursor-cli", room_snapshot=room,
                            prompt="Can Cursor be assigned as reviewer?", corpus_ids=[])
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=10),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
        ),
        timeout=15.0,
    )

    assert final.status == "completed"
    msgs = [e for e in store.read_run(meta.id)[1] if e.type == EventType.MEMBER_MESSAGE]
    assert len(msgs) == 1
    assert msgs[0].payload["content"] == "Cursor can take this coding role."


@pytest.mark.asyncio
async def test_cli_member_without_room_residency_does_not_mismatch(
    tmp_errorta_home, runs_dir_path, monkeypatch
):
    """Regression: a CLI (remote) member in a room with NO ``residency`` pinned
    must dispatch without ``payload_route_mismatch``.

    Before the fix, the engine forced a room-wide ``destination_scope=local``
    default whenever the room didn't set one, flattening the CLI member's
    payload onto local egress while its route classified ``remote`` → the
    gateway-boundary alignment check failed closed. That surfaced to the user
    as ``Run failed: gateway_error`` with detail ``payload_route_mismatch:
    local egress + remote destination``. The same room with an explicit
    ``residency: {destination_scope: remote}`` already worked; this locks the
    common case where the user never set residency at all."""
    import errorta_model_gateway.providers._cli_common as common

    async def fake_exec(*argv, **kwargs):
        return _FakeProc(_claude_json("Reverse it with three pointers."))

    monkeypatch.setattr(common.asyncio, "create_subprocess_exec", fake_exec)

    # No residency, no corpus_policy, no remote markers — a plain room the user
    # added a CLI member to (exactly the demo-3llm shape that failed).
    room = {
        "id": "rm-noresidency",
        "allow_full_context": True,
        "members": [
            {
                "id": "Claude", "enabled": True, "role": "member",
                "provider": "claude_cli", "model": "haiku",
                "gateway_route_id": "claude_cli.haiku",
                "context_access": "prompt_only", "transcript_access": "none",
            },
        ],
        "topology": {"kind": "round_robin"},
        "finalization_policy": {"mode": "transcript_only", "finalizer_member_id": None},
    }

    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(room_id="rm-noresidency", room_snapshot=room,
                            prompt="reverse a linked list?", corpus_ids=[])

    final = await asyncio.wait_for(
        build_and_run(
            run_store=store, run_meta=meta,
            policy=SchedulerPolicy(max_rounds=1, max_messages_per_member=1,
                                   per_turn_timeout_seconds=10),
            gateway_meta=_FakeMeta(), hardware_scan_present=True,
        ),
        timeout=15.0,
    )

    events = store.read_run(meta.id)[1]
    failures = [
        e for e in events
        if e.type == EventType.MEMBER_FAILED
        and "payload_route_mismatch" in str((e.payload or {}).get("detail", ""))
    ]
    assert not failures, [e.payload for e in failures]
    msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    assert msgs and msgs[0].payload["content"] == "Reverse it with three pointers."
    assert final.status == "completed"
