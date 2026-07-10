"""Invariant 5 backstop: gateway re-validates payload route alignment
before any dispatch attempt. Locks F031-3c boundary semantics.
"""
from __future__ import annotations

import pytest

from errorta_council.gateway_local import (
    FatalError,
    verify_payload_route_alignment,
)


def test_local_local_route_accepted():
    verify_payload_route_alignment(
        destination_scope="local", egress_class="local", route_id="local.ollama.x",
    )


def test_fake_local_route_accepted():
    verify_payload_route_alignment(
        destination_scope="local", egress_class="local", route_id="fake.local.stub-model",
    )


def test_remote_destination_with_local_egress_class_blocks():
    with pytest.raises(FatalError) as exc:
        verify_payload_route_alignment(
            destination_scope="remote", egress_class="local",
            route_id="local.ollama.x",
        )
    assert "payload_route_mismatch" in str(exc.value)


def test_local_destination_with_remote_route_blocks():
    with pytest.raises(FatalError) as exc:
        verify_payload_route_alignment(
            destination_scope="local", egress_class="local",
            route_id="anthropic.claude-sonnet-4-6",
        )
    assert "payload_route_mismatch" in str(exc.value)


def test_unknown_destination_blocks():
    with pytest.raises(FatalError) as exc:
        verify_payload_route_alignment(
            destination_scope="experimental_v3",
            egress_class="local", route_id="local.ollama.x",
        )
    assert "unknown_destination" in str(exc.value)


def test_unknown_egress_class_blocks():
    with pytest.raises(FatalError) as exc:
        verify_payload_route_alignment(
            destination_scope="local",
            egress_class="purple", route_id="local.ollama.x",
        )
    assert "unknown_egress_class" in str(exc.value)
