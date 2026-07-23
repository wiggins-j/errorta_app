"""R7 — import-time side effects + module-global state.

Locks the three properties R7 buys:

1. Importing ``errorta_cli.registry`` / ``errorta_cli.app`` does NOT register any
   commands until ``ensure_registered()`` is called — verified in a *fresh*
   subprocess so the in-process autouse priming (conftest) can't mask it.
2. ``ensure_registered()`` is idempotent (call twice → same set, no argv dupes).
3. Two independent Typer invocations do not share ``_Globals`` state — the
   per-invocation context object replaces the old mutable module singleton.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from errorta_cli import app as app_module

_REPO_PYTHON = Path(__file__).resolve().parents[2]  # .../python


def _run_in_subprocess(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a pristine interpreter rooted at the repo's python dir.

    A subprocess is the honest way to observe pre-registration state: module-level
    registry population persists for a whole pytest session, and the autouse
    conftest fixture primes it before every in-process test.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=str(_REPO_PYTHON),
        capture_output=True,
        text=True,
    )


# --------------------------------------------------------------------------- #
# 1. Import has no registration side effect.
# --------------------------------------------------------------------------- #

def test_importing_registry_does_not_register_until_ensure() -> None:
    result = _run_in_subprocess(
        """
        from errorta_cli import registry

        # Pure import: the registry is empty until asked.
        assert registry.names() == (), f"import registered: {registry.names()}"

        registry.ensure_registered()
        populated = registry.names()
        assert populated, "ensure_registered() populated nothing"

        # Idempotent: a second call is a no-op, same set, no duplicates.
        registry.ensure_registered()
        assert registry.names() == populated, "second ensure_registered() drifted"
        print("OK", len(populated))
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK"), result.stdout


def test_importing_app_does_not_build_argv_surface_until_ensure() -> None:
    result = _run_in_subprocess(
        """
        from errorta_cli import app

        def argv_names():
            return {c.name for c in app.app.registered_commands}

        before = argv_names()
        # The two statically-decorated built-ins exist at import; the registry-
        # derived argv commands (e.g. `status`) do NOT until ensure_registered().
        assert "__serve__" in before
        assert "status" not in before, f"import built argv: {before}"

        app.ensure_registered()
        after = argv_names()
        assert "status" in after, "ensure_registered() did not build argv"

        # Idempotent: no duplicate argv entries on a second call.
        count = len(app.app.registered_commands)
        app.ensure_registered()
        assert argv_names() == after
        assert len(app.app.registered_commands) == count, "duplicate argv commands"
        print("OK", len(after))
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("OK"), result.stdout


def test_ensure_registered_does_not_add_duplicate_argv_commands() -> None:
    """In-process: calling ensure_registered() repeatedly keeps the argv count flat."""
    app_module.ensure_registered()
    first = list(app_module.app.registered_commands)
    app_module.ensure_registered()
    app_module.ensure_registered()
    second = list(app_module.app.registered_commands)
    assert len(first) == len(second)
    # No name appears twice.
    names = [c.name for c in second]
    assert len(names) == len(set(names)), names


# --------------------------------------------------------------------------- #
# 2. Per-invocation globals: no cross-invocation leak.
# --------------------------------------------------------------------------- #

def test_two_invocations_do_not_share_globals_state(monkeypatch) -> None:
    """Each ``app()`` invocation gets a FRESH ``_Globals`` (via ctx.obj), so a
    ``--home`` set on one invocation is invisible to the next."""
    app_module.ensure_registered()

    seen: list[tuple[str, app_module._Globals]] = []

    def _fake_run(name: str, raw_args: list[str], g: app_module._Globals) -> None:
        seen.append((name, g))

    monkeypatch.setattr(app_module, "_run_registry_command", _fake_run)

    runner = CliRunner()
    r1 = runner.invoke(app_module.app, ["--home", "/tmp/isolated-a", "status"])
    r2 = runner.invoke(app_module.app, ["status"])

    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output
    assert len(seen) == 2, seen
    (_, g1), (_, g2) = seen

    assert g1 is not g2                       # a distinct object per invocation
    assert g1.home == "/tmp/isolated-a"       # first saw its own --home
    assert g2.home is None                    # …which did NOT leak into the second
