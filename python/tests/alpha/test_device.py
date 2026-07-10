"""Device identity: generation, idempotency, corruption recovery, 0600."""
from __future__ import annotations

import stat
import uuid

from errorta_alpha import device
from errorta_app.paths import alpha_device_path


def test_first_run_generates_valid_uuid(alpha_home):
    assert device.read_device_id() is None
    did = device.get_or_create_device_id()
    uuid.UUID(did)  # parses -> valid v4 string
    assert device.read_device_id() == did


def test_idempotent_across_calls(alpha_home):
    a = device.get_or_create_device_id()
    b = device.get_or_create_device_id()
    assert a == b


def test_corrupt_file_is_regenerated(alpha_home):
    alpha_device_path().write_text("{ not json", encoding="utf-8")
    did = device.get_or_create_device_id()
    uuid.UUID(did)
    assert device.read_device_id() == did


def test_non_uuid_value_is_rejected_and_regenerated(alpha_home):
    alpha_device_path().write_text('{"device_id": "not-a-uuid"}', encoding="utf-8")
    assert device.read_device_id() is None
    did = device.get_or_create_device_id()
    uuid.UUID(did)


def test_device_file_is_owner_only(alpha_home):
    device.get_or_create_device_id()
    mode = stat.S_IMODE(alpha_device_path().stat().st_mode)
    assert mode == 0o600
