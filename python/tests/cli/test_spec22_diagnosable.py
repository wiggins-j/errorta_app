"""Spec 22 — diagnosable failures, CLI side.

The regression these lock: ``_launch`` sent the sidecar's stdout AND stderr to
``DEVNULL``, so a ``500`` on project creation survived three debugging sessions
with nothing but an exit code to show for it. Every assertion below is about
evidence *surviving*.

* Item 1 — a CLI-spawned sidecar's output lands in ``${ERRORTA_HOME}/logs/sidecar.log``,
  the file is rotated (two generations, bounded forever), the spawn env's bearer
  token never appears in it, and ``errorta status`` prints the path.
* Item 2 — the CLI appends the sidecar's ``error_id`` to a ``500``.
* Item 4 — ``errorta delete`` clears ``cli-team-drafts/<id>.json``, and a refused
  (``409``) delete leaves the draft alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from errorta_cli import config, registry, sidecar, teamdraft
from errorta_cli.client import SidecarClient
from errorta_cli.errors import CliError, LockBusy, SidecarUnreachable

# --------------------------------------------------------------------------- #
# Item 1 — the sidecar's stdio reaches disk.
# --------------------------------------------------------------------------- #

_MARKER = "SPEC22-CHILD-STDERR-MARKER"


def _child_argv(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def test_dead_child_leaves_its_output_on_disk(monkeypatch, tmp_path: Path) -> None:
    """The exact regression: today the marker is unrecoverable.

    A child that prints a traceback-shaped line to stderr and exits non-zero used
    to yield `the sidecar exited during startup (code 3)` and nothing else.
    """
    monkeypatch.setattr(
        sidecar, "_serve_argv",
        lambda: _child_argv(
            f"import sys; sys.stderr.write({_MARKER!r} + chr(10)); sys.exit(3)"),
    )
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)

    with pytest.raises(SidecarUnreachable) as excinfo:
        sidecar.spawn(tmp_path, our_commit="abc")

    log = config.sidecar_log_path(tmp_path)
    assert log.is_file()
    assert _MARKER in log.read_text("utf-8")
    # Item 5's cross-cutting check: the path the error names is the path written.
    assert str(log) in str(excinfo.value)


def test_stdout_and_stderr_are_merged(monkeypatch, tmp_path: Path) -> None:
    """One file, interleaved — a traceback split across two is worse than either."""
    monkeypatch.setattr(
        sidecar, "_serve_argv",
        lambda: _child_argv(
            "import sys; sys.stdout.write('OUT\\n'); sys.stdout.flush(); "
            "sys.stderr.write('ERR\\n'); sys.exit(1)"),
    )
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)

    with pytest.raises(SidecarUnreachable):
        sidecar.spawn(tmp_path, our_commit="abc")

    text = config.sidecar_log_path(tmp_path).read_text("utf-8")
    assert "OUT" in text and "ERR" in text


def test_spawn_env_bearer_token_never_reaches_the_log(
    monkeypatch, tmp_path: Path
) -> None:
    """Standing assertion, not an assumption (the whole point of Item 1).

    ``ERRORTA_SIDECAR_TOKEN`` travels by env, not argv, so it appears in no
    process listing and in no spawn line. Prove it rather than inspect it.
    """
    seen: dict[str, str] = {}
    real_launch = sidecar._launch

    def spy(argv, env, **kw):
        seen["token"] = env[sidecar.SIDECAR_TOKEN_ENV]
        return real_launch(argv, env, **kw)

    monkeypatch.setattr(sidecar, "_launch", spy)
    monkeypatch.setattr(
        sidecar, "_serve_argv",
        lambda: _child_argv("import os, sys; sys.stderr.write(str(sorted(os.environ))"
                            "[:200]); sys.exit(2)"),
    )
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)

    with pytest.raises(SidecarUnreachable):
        sidecar.spawn(tmp_path, our_commit="abc")

    text = config.sidecar_log_path(tmp_path).read_text("utf-8")
    assert seen["token"] and seen["token"] not in text


def test_log_rotates_at_spawn_and_stays_bounded(monkeypatch, tmp_path: Path) -> None:
    """One generation kept; total bytes bounded across repeated spawns."""
    cap = 2048
    monkeypatch.setattr(sidecar, "_LOG_ROTATE_BYTES", cap)
    monkeypatch.setattr(
        sidecar, "_serve_argv",
        lambda: _child_argv(f"import sys; sys.stderr.write('x' * {cap}); sys.exit(1)"),
    )
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)

    log = config.sidecar_log_path(tmp_path)
    rotated = log.with_name(log.name + ".1")

    for _ in range(5):
        with pytest.raises(SidecarUnreachable):
            sidecar.spawn(tmp_path, our_commit="abc")

    assert rotated.is_file()                       # a generation was kept
    assert log.stat().st_size <= cap * 2           # the live file never runs away
    total = log.stat().st_size + rotated.stat().st_size
    assert total <= cap * 4, total                 # the unbounded-growth lock
    # Exactly two generations, forever — no `.2`, no `.3`.
    assert not (log.parent / (log.name + ".2")).exists()


def test_unwritable_logs_dir_fails_open(monkeypatch, tmp_path: Path, capsys) -> None:
    """A CLI that refuses to run because it cannot open a log is a worse product."""
    monkeypatch.setattr(sidecar, "_warned_log_degraded", False)
    # A *file* where the logs dir should be: mkdir and open both fail.
    (tmp_path / "logs").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        sidecar, "_serve_argv", lambda: _child_argv("import sys; sys.exit(7)"))
    monkeypatch.setattr(sidecar, "probe_healthz", lambda *a, **k: None)

    with pytest.raises(SidecarUnreachable):
        sidecar.spawn(tmp_path, our_commit="abc")

    assert "could not open the sidecar log" in capsys.readouterr().err


def test_status_prints_the_log_path(make_ctx, tmp_path: Path) -> None:
    """Item 5: every path the CLI prints must exist and be written."""
    from .conftest import RouteClient

    ctx = make_ctx()
    client = RouteClient({"/healthz": {"service": "errorta", "version": "0"}})
    payload, text = registry.dispatch("status", client, ctx, [])
    assert payload["log_path"] == str(config.sidecar_log_path(tmp_path))
    # (rich soft-wraps a long path across lines, so match on the label + tail)
    assert "log:" in text and "logs/sidecar.log" in text.replace("\n", "")


# --------------------------------------------------------------------------- #
# Item 2 — the CLI surfaces the sidecar's correlation id.
# --------------------------------------------------------------------------- #

def _raise_500(body: dict) -> CliError:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=body)

    with SidecarClient("http://127.0.0.1:9",
                       transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CliError) as excinfo:
            client.get_json("/boom")
    return excinfo.value


def test_cli_error_carries_the_error_id() -> None:
    err = _raise_500({"detail": {
        "code": "internal_error",
        "error_id": "e-0123456789ab",
        "message": "the sidecar hit an unhandled error",
        "hint": "grep e-0123456789ab in /tmp/logs/sidecar.log",
    }})
    assert "e-0123456789ab" in str(err)
    assert err.code == "internal_error"


def test_a_500_without_an_error_id_is_unchanged() -> None:
    """Back-compat with an older sidecar (the no-false-positives half)."""
    err = _raise_500({"detail": "Internal Server Error"})
    assert str(err) == "sidecar returned 500: 'Internal Server Error'"
    assert "error id" not in str(err)


# --------------------------------------------------------------------------- #
# Item 4 — delete clears the CLI's own per-project state.
# --------------------------------------------------------------------------- #

def _seed_draft(home: Path, project_id: str) -> Path:
    teamdraft.save(home, project_id, {"members": [{"role": "pm"}], "room_id": None})
    path = teamdraft.draft_path(home, project_id)
    assert path.is_file()
    return path


def test_delete_clears_the_team_draft(make_ctx, tmp_path: Path) -> None:
    from .conftest import RouteClient

    ctx = make_ctx("acme")
    draft = _seed_draft(tmp_path, "acme")
    client = RouteClient(default={"deleted": True, "project_id": "acme"})

    registry.dispatch("delete", client, ctx, ["acme", "--yes"])

    assert not draft.exists()
    assert ("DELETE", "/coding/projects/acme") in client.calls
    assert ctx.project_id is None


def test_a_refused_delete_leaves_the_draft_intact(make_ctx, tmp_path: Path) -> None:
    """A 409 ("project run is still active") must not destroy a live draft."""
    ctx = make_ctx("acme")
    draft = _seed_draft(tmp_path, "acme")

    class _Refusing:
        base_url = "http://127.0.0.1:9"

        def delete_json(self, path: str, *, params: dict | None = None):
            raise LockBusy("project run is still active")

    with pytest.raises(LockBusy):
        registry.dispatch("delete", _Refusing(), ctx, ["acme", "--yes"])

    assert draft.is_file()
    assert ctx.project_id == "acme"
