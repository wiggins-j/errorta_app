from __future__ import annotations

import asyncio

import pytest

from errorta_council.engine import build_and_run
from errorta_council.limits import SchedulerPolicy
from errorta_council.run_store import RunStore
from errorta_council.schema import EventType


class _FakeGatewayMeta:
    async def is_reachable(self) -> bool:
        return True
    async def list_installed_models(self) -> list[str]:
        return ["stub-model"]


@pytest.mark.asyncio
async def test_build_and_run_two_fake_members(tmp_errorta_home, runs_dir_path) -> None:
    store = RunStore(runs_dir=runs_dir_path)
    meta = store.create_run(
        room_id="rm",
        room_snapshot={
            "id": "rm",
            "members": [
                {"id": "m1", "enabled": True, "role": "member", "provider": "fake", "model": "stub-model"},
                {"id": "m2", "enabled": True, "role": "member", "provider": "fake", "model": "stub-model"},
            ],
        },
        prompt="hi",
        corpus_ids=[],
    )
    final = await asyncio.wait_for(
        build_and_run(
            run_store=store,
            run_meta=meta,
            policy=SchedulerPolicy(
                max_rounds=1, max_messages_per_member=1, per_turn_timeout_seconds=5,
            ),
            gateway_meta=_FakeGatewayMeta(),
            hardware_scan_present=True,
        ),
        timeout=2.0,
    )
    assert final.status == "completed"
    _, events = store.read_run(meta.id)
    member_msgs = [e for e in events if e.type == EventType.MEMBER_MESSAGE]
    assert [e.member_id for e in member_msgs] == ["m1", "m2"]
