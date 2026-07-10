"""F101-03 S8 — declared-but-not-built launcher extension points."""
from __future__ import annotations

import pytest

from errorta_council.coding.runtime_launchers import (
    EmulationLauncher,
    MobileLauncher,
    get_launcher,
)
from errorta_council.coding.runtime_process import RuntimeProcessError


@pytest.mark.parametrize("modality,cls", [
    ("emulation", EmulationLauncher),
    ("mobile", MobileLauncher),
])
def test_stub_registered_and_refuses(modality, cls):
    launcher = get_launcher(modality)
    assert isinstance(launcher, cls)
    with pytest.raises(RuntimeProcessError) as exc:
        launcher.launch(None, None)
    assert f"{modality}_not_built" in str(exc.value)


def test_no_detector_emits_stub_modalities():
    # These are documented extension points, not runnable today — nothing in the
    # kind->modality map routes to them, so the front door never dispatches one.
    from errorta_council.coding.runtime_resolve import _MODALITY_BY_KIND
    assert "emulation" not in _MODALITY_BY_KIND.values()
    assert "mobile" not in _MODALITY_BY_KIND.values()
