"""F149 — `errorta new <id> [location]` root resolution, the cd handshake, and
the `errorta shell-init` hook. Pure-function coverage (no sidecar)."""
from __future__ import annotations

from pathlib import Path

import pytest

from errorta_cli.commands.project import _resolve_new_root, emit_cd_target
from errorta_cli.errors import CliError
from errorta_cli.shellinit import render_hook

# --- _resolve_new_root -------------------------------------------------------

_BASE = Path("/base/dir")


def test_default_is_none() -> None:
    assert _resolve_new_root({}, _BASE) is None
    assert _resolve_new_root({"location": "", "delivery-root": ""}, _BASE) is None


def test_absolute_location_is_passed_through(tmp_path: Path) -> None:
    # Absolute + not symlink-resolved (the server canonicalizes).
    got = _resolve_new_root({"location": "/tmp/x"}, _BASE)
    assert got == "/tmp/x"


def test_relative_location_is_absolutized_against_base() -> None:
    got = _resolve_new_root({"location": "sub/proj"}, _BASE)
    assert got == str(_BASE / "sub" / "proj")


def test_tilde_is_expanded() -> None:
    got = _resolve_new_root({"location": "~/some-nl-dir"}, _BASE)
    assert got is not None and "~" not in got
    assert got.startswith(str(Path.home()))


def test_here_uses_base_dir(tmp_path: Path) -> None:
    got = _resolve_new_root({"here": True}, tmp_path)
    assert got == str(tmp_path)


def test_here_conflicts_with_location() -> None:
    with pytest.raises(CliError):
        _resolve_new_root({"here": True, "location": "/tmp/x"}, _BASE)


def test_location_and_delivery_root_disagree() -> None:
    with pytest.raises(CliError):
        _resolve_new_root({"location": "/tmp/a", "delivery-root": "/tmp/b"}, _BASE)


def test_location_and_delivery_root_agree() -> None:
    got = _resolve_new_root({"location": "/tmp/x", "delivery-root": "/tmp/x"}, _BASE)
    assert got == "/tmp/x"


# --- emit_cd_target ----------------------------------------------------------

def test_emit_writes_when_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cd = tmp_path / "cd.txt"
    monkeypatch.setenv("ERRORTA_CD_FILE", str(cd))
    emit_cd_target(tmp_path / "proj")
    assert cd.read_text().strip() == str(tmp_path / "proj")


def test_emit_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERRORTA_CD_FILE", raising=False)
    emit_cd_target("/tmp/whatever")  # must not raise
    emit_cd_target(None)


# --- render_hook -------------------------------------------------------------

@pytest.mark.parametrize("shell", ["zsh", "bash"])
def test_render_hook_has_the_pieces(shell: str) -> None:
    out = render_hook(shell)
    assert "errorta()" in out
    assert "ERRORTA_CD_FILE" in out
    assert "builtin cd -- " in out          # quoted cd (default path has a space)
    assert "command errorta" in out         # calls the real binary, not itself


def test_render_hook_rejects_unknown_shell() -> None:
    with pytest.raises(CliError):
        render_hook("fish")
