"""Operator-declared, UNSANDBOXED acceptance gate (spec 2026-08-23-trusted-gate).

A trusted gate exists only because a human put a file in ``$ERRORTA_HOME/gates``
with the right owner and mode. The engine never writes there; Task 5 of this
slice lands the grep test that keeps it so
(``tests/test_gates_dir_is_operator_only.py``). Same provenance bar as a
live-run profile; strictly less power than one.

Hard links and the stat-then-read TOCTOU window are accepted at this trust
level: a same-uid attacker who could already write a same-owner, same-mode
file into ``gates/`` could just write a valid trusted gate directly, so
defending against either buys nothing. Likewise an absolute ``argv[0]`` is
deliberately unconstrained by design — the file is operator-authored, not
attacker-supplied — and the shell-metacharacter filter on argv is hygiene
against accidental shell interpretation downstream, not a sandbox boundary.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from errorta_app.paths import errorta_home
from errorta_export.safe_path import UnsafePathError, safe_segment
from errorta_tools.runner.env import is_secret_env_name

BANNED_TOKENS = ("--ignore-risk-budget", "--no-safety-plane")
RELATIVE_ARGV0 = ("./gradlew", "./mvnw")
SCOPES = ("unit", "acceptance")
MAX_COMMANDS = 8
MAX_ENV = 32
MAX_TIMEOUT_S = 1800
ALLOWED_MODES = (0o600, 0o640, 0o644)
_SHELL_CHARS = re.compile(r"[$`|;&<>]")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TOP_KEYS = {"version", "created_by", "project_id", "commands", "env"}
_CMD_KEYS = {"id", "argv", "cwd", "timeout_seconds", "scope"}
_ENV_KEYS = {"passthrough"}


class TrustedGateError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class TrustedCommand:
    id: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    scope: str


@dataclass(frozen=True)
class TrustedGate:
    project_id: str
    path: str
    commands: tuple[TrustedCommand, ...]
    env_passthrough: tuple[str, ...]


def gates_dir() -> Path:
    return errorta_home() / "gates"


def gate_path(project_id: str) -> Path:
    try:
        safe_segment(project_id)
    except UnsafePathError as exc:
        raise TrustedGateError("bad_project_id", str(exc)) from None
    return gates_dir() / f"{project_id}.yaml"


def _file_guard(path: Path) -> None:
    if path.is_symlink():
        raise TrustedGateError("gate_is_symlink", str(path))
    # A symlinked gates/ directory defeats a resolve()-both-sides containment
    # check: gate_path()'s parent and gates_dir() would resolve through the
    # same symlink to the same place either way, so the comparison below is
    # only meaningful once we know the directory itself isn't a symlink.
    if gates_dir().is_symlink():
        raise TrustedGateError("gates_dir_is_symlink", str(gates_dir()))
    if path.parent.resolve() != gates_dir().resolve():
        raise TrustedGateError("gate_outside_dir", str(path))
    st = path.stat()
    if not stat.S_ISREG(st.st_mode):
        raise TrustedGateError("gate_not_regular_file", str(path))
    if st.st_uid != os.getuid():
        raise TrustedGateError("gate_not_owned", str(path))
    if stat.S_IMODE(st.st_mode) not in ALLOWED_MODES:
        raise TrustedGateError("gate_mode_insecure", oct(stat.S_IMODE(st.st_mode)))


def _int(value: Any, *, code: str, lo: int, hi: int, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
        raise TrustedGateError(code, where)
    return value


def _command(raw: Any, *, where: str) -> TrustedCommand:
    if not isinstance(raw, dict):
        raise TrustedGateError("bad_command", where)
    extra = set(raw) - _CMD_KEYS
    if extra:
        raise TrustedGateError("unknown_key", f"{where}: {sorted(extra)}")
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise TrustedGateError("bad_command_id", where)
    try:
        safe_segment(cid)
    except UnsafePathError:
        raise TrustedGateError("bad_command_id", where) from None
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise TrustedGateError("bad_argv", where)
    for a in argv:
        if _SHELL_CHARS.search(a) or "\x00" in a:
            raise TrustedGateError("shell_chars", f"{where}: {a!r}")
        if a in BANNED_TOKENS:
            raise TrustedGateError("banned_token", f"{where}: {a}")
    if not (os.path.isabs(argv[0]) or argv[0] in RELATIVE_ARGV0):
        raise TrustedGateError("argv0_not_absolute", f"{where}: {argv[0]!r}")
    cwd = raw.get("cwd", ".")
    if (not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd
            or cwd.startswith(("/", "~")) or ".." in Path(cwd).parts):
        raise TrustedGateError("bad_cwd", f"{where}: {cwd!r}")
    timeout = _int(raw.get("timeout_seconds"), code="bad_timeout", lo=1, hi=MAX_TIMEOUT_S,
                   where=where)
    scope = raw.get("scope", "unit")
    if scope not in SCOPES:
        raise TrustedGateError("bad_scope", f"{where}: {scope!r}")
    return TrustedCommand(id=cid, argv=tuple(argv), cwd=cwd, timeout_seconds=timeout, scope=scope)


def _env(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise TrustedGateError("bad_env", repr(raw)[:80])
    extra = set(raw) - _ENV_KEYS
    if extra:
        raise TrustedGateError("unknown_key", f"env: {sorted(extra)}")
    names = raw.get("passthrough", [])
    if not isinstance(names, list):
        raise TrustedGateError("bad_env_name", repr(names)[:80])
    if len(names) > MAX_ENV:
        raise TrustedGateError("too_many_env", str(len(names)))
    out: list[str] = []
    for n in names:
        if not isinstance(n, str) or not _ENV_NAME.match(n):
            raise TrustedGateError("bad_env_name", repr(n)[:80])
        if is_secret_env_name(n):
            raise TrustedGateError("secret_env_name", n)
        out.append(n)
    return tuple(out)


def _parse_gate(project_id: str, path: Path) -> TrustedGate:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise TrustedGateError("bad_yaml", str(exc)[:120]) from None
    if not isinstance(doc, dict):
        raise TrustedGateError("bad_yaml", "top level is not a mapping")
    extra = set(doc) - _TOP_KEYS
    if extra:
        raise TrustedGateError("unknown_key", f"top: {sorted(extra)}")
    if doc.get("version") != 1:
        raise TrustedGateError("bad_version", repr(doc.get("version")))
    if doc.get("created_by") != "operator":
        raise TrustedGateError("created_by_not_operator", repr(doc.get("created_by")))
    if doc.get("project_id") != project_id:
        raise TrustedGateError("project_id_mismatch", repr(doc.get("project_id")))
    raw_cmds = doc.get("commands")
    if not isinstance(raw_cmds, list) or not raw_cmds:
        raise TrustedGateError("no_commands", "")
    if len(raw_cmds) > MAX_COMMANDS:
        raise TrustedGateError("too_many_commands", str(len(raw_cmds)))
    commands = tuple(_command(c, where=f"commands[{i}]") for i, c in enumerate(raw_cmds))
    if len({c.id for c in commands}) != len(commands):
        raise TrustedGateError("bad_command_id", "duplicate id")
    return TrustedGate(project_id=project_id, path=str(path), commands=commands,
                       env_passthrough=_env(doc.get("env")))


def load_trusted_gate(project_id: str) -> TrustedGate | None:
    path = gate_path(project_id)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        _file_guard(path)
        return _parse_gate(project_id, path)
    except TrustedGateError:
        raise
    except Exception as exc:  # last line of defence: any wrong-shaped YAML,
        # a stat()-race/permission error from _file_guard, or any other
        # unanticipated failure must still fail closed as a TrustedGateError,
        # not crash the caller (mirrors errorta_liverun/profile.py::load_profile).
        raise TrustedGateError("gate_malformed", f"{type(exc).__name__}: {exc}"[:120]) from None


def registry_view(gate: TrustedGate) -> dict[str, dict[str, Any]]:
    """The gate as the registry shape every reader already understands, each
    spec tagged ``tier: trusted`` so the executor knows which runner to use.
    ``env_passthrough`` is carried on every spec (not just at the gate level)
    because the registry view is the only shape ``run_test_commands`` reads —
    it has no access to the ``TrustedGate`` object itself when it dispatches
    each command to the trusted executor."""
    return {c.id: {"argv": list(c.argv), "cwd": c.cwd, "timeout_seconds": c.timeout_seconds,
                   "scope": c.scope, "tier": "trusted",
                   "env_passthrough": list(gate.env_passthrough)} for c in gate.commands}


def invalid_registry_view(project_id: str, code: str) -> dict[str, dict[str, Any]]:
    """A present-but-invalid file is LOUD: one marker command the executor turns
    into a blocked record, so the project neither loses its gate silently nor
    falls back to the sandboxed tier."""
    return {"trusted-gate": {"tier": "trusted", "invalid": code, "argv": [], "cwd": ".",
                             "timeout_seconds": 1, "scope": "unit", "env_passthrough": []}}


def passthrough_env(names: tuple[str, ...]) -> dict[str, str]:
    """Thin re-export: the actual environment-copy logic lives in
    ``errorta_tools.runner.trusted_exec`` alongside the ``subprocess`` launch
    it feeds — this package may not import ``subprocess`` itself (see
    ``tests/council/test_tool_runner_local.py`` and
    ``tests/council/test_toolgateway_slice1.py``)."""
    from errorta_tools.runner.trusted_exec import passthrough_env as _passthrough_env
    return _passthrough_env(names)


def run_trusted_command(spec: dict[str, Any], *, command_id: str, workspace_root: Path,
                        env_passthrough: tuple[str, ...], should_cancel=None) -> Any:
    """Run ONE trusted command with no sandbox wrapper and the declared
    passthrough environment, returning a ``TestRunResult`` so a caller can't
    tell which tier produced it from the shape alone.

    The actual unsandboxed launch (``subprocess.Popen``, kill-group,
    bounded-drain) lives in ``errorta_tools.runner.trusted_exec`` — this
    package (``errorta_council``) is not allowed to import ``subprocess``
    itself, the same boundary ``errorta_tools/runner/local.py`` enforces for
    the sandboxed tier. This function only delegates and converts the plain
    ``TrustedExecResult`` it gets back into the council's ``TestRunResult``."""
    from errorta_council.coding.testing import TestRunResult
    from errorta_tools.runner.trusted_exec import run_trusted_command as _run_trusted_command

    r = _run_trusted_command(spec, command_id=command_id, workspace_root=workspace_root,
                             env_passthrough=env_passthrough, should_cancel=should_cancel)
    return TestRunResult(
        command_id=r.command_id, argv_sha256=r.argv_sha256, status=r.status,
        exit_code=r.exit_code, passed=r.passed, duration_ms=r.duration_ms,
        stdout_sha256=r.stdout_sha256, stdout_preview=r.stdout_preview,
        stderr_preview=r.stderr_preview, reason=r.reason)


__all__ = ["TrustedGate", "TrustedGateError", "TrustedCommand", "gate_path", "gates_dir",
           "invalid_registry_view", "load_trusted_gate", "passthrough_env", "registry_view",
           "run_trusted_command"]
