"""Opt-in release-venv assertion for F-INFRA-01 Slice (g).

Default-skipped so the normal `pytest` run from the dev venv (which has
an editable AIAR) stays green. The operator opts in inside the
`python/.venv-release` venv built by ``scripts/build-sidecar-release.sh``
by exporting ``ERRORTA_RELEASE_VENV_CHECK=1`` before invoking pytest.

When opted in, this test imports ``aiar`` and asserts the
``_local_aiar_pin`` probe classifies the install as ``source: "pinned"``
— proving the ``[release]`` extras pin survives the release-venv build
path, not just an editable dev install.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.skipif(
    not os.getenv("ERRORTA_RELEASE_VENV_CHECK"),
    reason=(
        "Opt-in release-venv probe. Export ERRORTA_RELEASE_VENV_CHECK=1 "
        "inside python/.venv-release to run."
    ),
)
def test_release_venv_reports_pinned_aiar() -> None:
    """The release venv must report aiar_pin.source == 'pinned'.

    Run after ``scripts/build-sidecar-release.sh`` provisions the venv:

        ERRORTA_RELEASE_VENV_CHECK=1 \
            python/.venv-release/bin/pytest \
            python/tests/test_aiar_pin_release_install.py -v
    """
    # Imported here so the dev venv (where the import-name vs dist-name
    # split is differently shaped under editable AIAR) is unaffected.
    from errorta_app.health.aiar_pin import _local_aiar_pin

    # The release venv must actually have aiar installed (from PyPI via
    # the [release] extras). If this import fails we want a clear
    # ImportError, not a quietly-skipped test.
    import aiar  # noqa: F401

    pin = _local_aiar_pin()

    assert pin["available"] is True, pin
    assert pin["version"] == "0.2.0", pin
    assert pin["source"] == "pinned", (
        f"expected 'pinned' inside the release venv but got {pin['source']!r}. "
        "If this says 'editable', the .venv-release accidentally shadowed the "
        "PyPI AIAR with an editable install. Delete .venv-release and re-run "
        "scripts/build-sidecar-release.sh."
    )
