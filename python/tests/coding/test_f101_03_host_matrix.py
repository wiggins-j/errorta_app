"""F101-03 S5 — the host/residency matrix (HostFacts + can_launch)."""
from __future__ import annotations

from errorta_council.coding.runtime import HostFacts
from errorta_council.coding.runtime_launchers import can_launch
from errorta_council.coding.runtime_resolve import LaunchPlan


def _plan(modality: str, **over) -> LaunchPlan:
    base = dict(
        modality=modality, profile_id="default", kind=modality, start=["x"],
        setup=[], working_dir=".", ports=[], health={}, env_required=[],
        grounded_by="detector", verified_paths=["x"])
    base.update(over)
    return LaunchPlan(**base)


LOCAL = HostFacts(has_display=True, os="macos", arch="arm64", is_remote=False)
HEADLESS = HostFacts(has_display=False, os="linux", arch="x86_64", is_remote=False)
REMOTE = HostFacts(has_display=False, os="linux", arch="x86_64", is_remote=True)


def test_headless_modalities_run_anywhere():
    for m in ("static", "server", "cli", "container"):
        assert can_launch(_plan(m), REMOTE) == (True, None)


def test_desktop_needs_a_display():
    assert can_launch(_plan("desktop"), LOCAL) == (True, None)
    ok, reason = can_launch(_plan("desktop"), HEADLESS)
    assert ok is False and reason == "no_display_on_host"


def test_desktop_refused_on_remote_host():
    ok, reason = can_launch(_plan("desktop"), REMOTE)
    assert ok is False and reason == "remote_host_has_no_display"


def test_binary_refused_on_remote_host():
    plan = _plan("binary", host_requirements={"os": "linux", "arch": "x86_64"})
    ok, reason = can_launch(plan, REMOTE)
    assert ok is False and reason == "cannot_ship_binary_to_remote_host"


def test_binary_foreign_arch_refused_locally():
    plan = _plan("binary", host_requirements={"os": "windows", "arch": "x86_64"})
    ok, reason = can_launch(plan, LOCAL)
    assert ok is False and "binary_host_mismatch" in reason


def test_binary_matching_host_ok():
    plan = _plan("binary", host_requirements={"os": "macos", "arch": "arm64"})
    assert can_launch(plan, LOCAL) == (True, None)


def test_local_host_facts_shape():
    host = HostFacts.local()
    assert host.is_remote is False
    assert host.os in ("macos", "linux", "windows")
    assert isinstance(host.has_display, bool)
    assert set(host.to_dict()) == {"has_display", "os", "arch", "is_remote"}
