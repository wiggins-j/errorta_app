# Trusted unsandboxed gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator declare, in a file only they can write, an unsandboxed acceptance gate for a project whose build cannot run inside the seatbelt (osrs-reaper's Gradle), so the coding loop and the live-run fix loop can verify and merge changes there.

**Architecture:** A new `errorta_council/coding/trusted_gate.py` loads and validates `$ERRORTA_HOME/gates/<project_id>.yaml` with the live-run profile's provenance guard and argv hygiene. `LedgerStore.get_test_commands()` returns that file's commands (each tagged `tier: "trusted"`) whenever the file exists, so every existing reader sees the gate unchanged; `testing.run_test_commands` routes tagged specs to an unsandboxed runner and records `sandbox: "trusted"`. Nothing under the engine packages writes to `gates/`.

**Tech Stack:** Python 3.12, PyYAML (already a dependency), subprocess, pytest. Tests from `python/`: `./.venv/bin/python -m pytest <path> -q`.

**Spec:** `docs/superpowers/specs/2026-08-23-trusted-gate-design.md`

## Global Constraints

- Repo is PUBLIC: no tokens, secrets, PII, or personal absolute paths in committed code/tests/docs (use `tmp_path` and `/r/...`-style fakes).
- No model-reachable way to declare, edit, or select a trusted gate: not a registry field, not a route body flag, not a Slack verb argument. Only the operator file selects the tier.
- Provenance guard, verbatim from the spec: not a symlink; resolves inside `errorta_home()/"gates"`; `st_uid == os.getuid()`; mode ∈ {0o600, 0o640, 0o644}; `version: 1`; `created_by: operator`; `project_id` equals the filename stem.
- Argv hygiene: list of non-empty strings; any of `$ \` | ; & < >` in any element rejected; `argv[0]` absolute, or exactly `./gradlew` / `./mvnw`; banned tokens `--ignore-risk-budget`, `--no-safety-plane`; `cwd` worktree-relative (no leading `/`, no `..`); `timeout_seconds` integer in `[1, 1800]`; `scope` ∈ {`unit`, `acceptance`} default `unit`; ≤ 8 commands; unknown keys at any level rejected.
- `env.passthrough`: names `^[A-Z][A-Z0-9_]*$`, ≤ 32 entries, `errorta_tools.runner.env.is_secret_env_name(name)` must be False. Values come from `os.environ` at run time; never from the file.
- A trusted gate **replaces** the sandboxed registry for that project; they are never merged. `require_sandbox` true + trusted file → blocked `sandbox_required_by_project`.
- A present-but-invalid file is loud: a blocked gate record `trusted_gate_invalid:<code>`; availability stays True.
- Merge gate, human-only verbs, caps, guarded paths untouched.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Never edit `~/.errorta` from a task.
- Known unrelated pre-existing failure on this machine: `tests/liverun/test_steps.py::test_remote_detach_uses_setsid_when_available` — ignore.

---

### Task 1: `trusted_gate.py` — load, guard, validate

**Files:**
- Create: `python/errorta_council/coding/trusted_gate.py`
- Test: `python/tests/council/test_trusted_gate.py`

**Interfaces:**
- Produces:
  ```python
  class TrustedGateError(Exception):      # .code: str, .detail: str
  @dataclass(frozen=True) class TrustedCommand: id: str; argv: tuple[str, ...]; cwd: str; timeout_seconds: int; scope: str
  @dataclass(frozen=True) class TrustedGate: project_id: str; path: str; commands: tuple[TrustedCommand, ...]; env_passthrough: tuple[str, ...]
  def gates_dir() -> Path                 # errorta_home() / "gates"
  def gate_path(project_id: str) -> Path  # gates_dir() / f"{project_id}.yaml"; project_id via errorta_export.safe_path.safe_segment
  def load_trusted_gate(project_id: str) -> TrustedGate | None   # None when no file; raises TrustedGateError otherwise
  def registry_view(gate: TrustedGate) -> dict[str, dict]        # {id: {"argv": [...], "cwd": ..., "timeout_seconds": n, "scope": s, "tier": "trusted"}}
  def invalid_registry_view(project_id: str, code: str) -> dict[str, dict]  # {"trusted-gate": {"tier": "trusted", "invalid": code, "argv": [], "cwd": ".", "timeout_seconds": 1, "scope": "unit"}}
  ```
- Error codes: `gate_is_symlink`, `gate_outside_dir`, `gate_not_owned`, `gate_mode_insecure`, `bad_yaml`, `bad_version`, `created_by_not_operator`, `project_id_mismatch`, `unknown_key`, `no_commands`, `too_many_commands`, `bad_command_id`, `bad_argv`, `shell_chars`, `banned_token`, `argv0_not_absolute`, `bad_cwd`, `bad_timeout`, `bad_scope`, `bad_env_name`, `secret_env_name`, `too_many_env`.

- [ ] **Step 1: Write the failing tests** — `python/tests/council/test_trusted_gate.py`:

```python
from __future__ import annotations
import os, stat
from pathlib import Path
import pytest
import yaml
from errorta_council.coding import trusted_gate as tg


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    (tmp_path / "gates").mkdir()
    return tmp_path


def _doc(**over) -> dict:
    d = {"version": 1, "created_by": "operator", "project_id": "reaper",
         "commands": [{"id": "compile", "argv": ["./gradlew", ":client:compileJava", "--offline"],
                       "cwd": ".", "timeout_seconds": 900, "scope": "unit"}],
         "env": {"passthrough": ["PATH", "HOME", "JAVA_HOME"]}}
    d.update(over)
    return d


def _write(home: Path, doc: dict, *, name: str = "reaper", mode: int = 0o600) -> Path:
    p = home / "gates" / f"{name}.yaml"
    p.write_text(yaml.safe_dump(doc))
    p.chmod(mode)
    return p


def test_no_file_is_none(_home: Path) -> None:
    assert tg.load_trusted_gate("reaper") is None


def test_valid_file_loads(_home: Path) -> None:
    _write(_home, _doc())
    g = tg.load_trusted_gate("reaper")
    assert g is not None and g.project_id == "reaper"
    assert g.commands[0].argv == ("./gradlew", ":client:compileJava", "--offline")
    assert g.commands[0].timeout_seconds == 900 and g.commands[0].scope == "unit"
    assert g.env_passthrough == ("PATH", "HOME", "JAVA_HOME")


@pytest.mark.parametrize("mode", [0o666, 0o604, 0o700, 0o664])
def test_insecure_mode_is_refused(_home: Path, mode: int) -> None:
    _write(_home, _doc(), mode=mode)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_mode_insecure"


def test_symlink_is_refused(_home: Path) -> None:
    real = _home / "elsewhere.yaml"
    real.write_text(yaml.safe_dump(_doc()))
    real.chmod(0o600)
    (_home / "gates" / "reaper.yaml").symlink_to(real)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_is_symlink"


def test_wrong_owner_is_refused(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(_home, _doc())
    monkeypatch.setattr(tg.os, "getuid", lambda: os.getuid() + 1)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "gate_not_owned"


@pytest.mark.parametrize("mutate,code", [
    (lambda d: d.update(version=2), "bad_version"),
    (lambda d: d.update(created_by="pm"), "created_by_not_operator"),
    (lambda d: d.update(project_id="other"), "project_id_mismatch"),
    (lambda d: d.update(extra=1), "unknown_key"),
    (lambda d: d.update(commands=[]), "no_commands"),
    (lambda d: d.update(commands=[d["commands"][0]] * 9), "too_many_commands"),
    (lambda d: d["commands"][0].update(id="bad/id"), "bad_command_id"),
    (lambda d: d["commands"][0].update(argv="./gradlew build"), "bad_argv"),
    (lambda d: d["commands"][0].update(argv=["./gradlew", "a;b"]), "shell_chars"),
    (lambda d: d["commands"][0].update(argv=["./gradlew", "--no-safety-plane"]), "banned_token"),
    (lambda d: d["commands"][0].update(argv=["gradlew", "build"]), "argv0_not_absolute"),
    (lambda d: d["commands"][0].update(cwd="/abs"), "bad_cwd"),
    (lambda d: d["commands"][0].update(cwd="../x"), "bad_cwd"),
    (lambda d: d["commands"][0].update(timeout_seconds=1801), "bad_timeout"),
    (lambda d: d["commands"][0].update(timeout_seconds=0), "bad_timeout"),
    (lambda d: d["commands"][0].update(timeout_seconds="900"), "bad_timeout"),
    (lambda d: d["commands"][0].update(scope="smoke"), "bad_scope"),
    (lambda d: d["commands"][0].update(bogus=1), "unknown_key"),
    (lambda d: d["env"].update(passthrough=["path"]), "bad_env_name"),
    (lambda d: d["env"].update(passthrough=["AWS_SECRET_ACCESS_KEY"]), "secret_env_name"),
    (lambda d: d["env"].update(passthrough=[f"V{i}" for i in range(33)]), "too_many_env"),
    (lambda d: d["env"].update(other=1), "unknown_key"),
])
def test_each_validation_rule(_home: Path, mutate, code: str) -> None:
    d = _doc()
    mutate(d)
    _write(_home, d)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == code


def test_absolute_argv0_and_mvnw_are_accepted(_home: Path) -> None:
    d = _doc(commands=[{"id": "a", "argv": ["/usr/bin/true"], "cwd": "sub", "timeout_seconds": 5},
                       {"id": "b", "argv": ["./mvnw", "-q", "test"], "cwd": ".", "timeout_seconds": 5}])
    _write(_home, d)
    g = tg.load_trusted_gate("reaper")
    assert [c.id for c in g.commands] == ["a", "b"] and g.commands[0].scope == "unit"


def test_bad_yaml_is_refused(_home: Path) -> None:
    p = _home / "gates" / "reaper.yaml"
    p.write_text("commands: [\n")
    p.chmod(0o600)
    with pytest.raises(tg.TrustedGateError) as ei:
        tg.load_trusted_gate("reaper")
    assert ei.value.code == "bad_yaml"


def test_registry_views() -> None:
    g = tg.TrustedGate(project_id="reaper", path="/x", env_passthrough=("PATH",),
                       commands=(tg.TrustedCommand("compile", ("./gradlew", "build"), ".", 900, "unit"),))
    assert tg.registry_view(g) == {"compile": {"argv": ["./gradlew", "build"], "cwd": ".",
                                               "timeout_seconds": 900, "scope": "unit", "tier": "trusted"}}
    inv = tg.invalid_registry_view("reaper", "gate_mode_insecure")
    assert inv["trusted-gate"]["tier"] == "trusted" and inv["trusted-gate"]["invalid"] == "gate_mode_insecure"
```

- [ ] **Step 2: Run** — `./.venv/bin/python -m pytest tests/council/test_trusted_gate.py -q`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `python/errorta_council/coding/trusted_gate.py`:

```python
"""Operator-declared, UNSANDBOXED acceptance gate (spec 2026-08-23-trusted-gate).

A trusted gate exists only because a human put a file in ``$ERRORTA_HOME/gates``
with the right owner and mode. The engine never writes there (a grep test keeps
it so). Same provenance bar as a live-run profile; strictly less power than one.
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
    try:
        path.resolve().relative_to(gates_dir().resolve())
    except ValueError:
        raise TrustedGateError("gate_outside_dir", str(path)) from None
    st = path.stat()
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
    try:
        if not isinstance(cid, str) or not cid:
            raise UnsafePathError(cid)
        safe_segment(cid)
    except UnsafePathError:
        raise TrustedGateError("bad_command_id", where) from None
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise TrustedGateError("bad_argv", where)
    for a in argv:
        if _SHELL_CHARS.search(a):
            raise TrustedGateError("shell_chars", f"{where}: {a!r}")
        if a in BANNED_TOKENS:
            raise TrustedGateError("banned_token", f"{where}: {a}")
    if not (os.path.isabs(argv[0]) or argv[0] in RELATIVE_ARGV0):
        raise TrustedGateError("argv0_not_absolute", f"{where}: {argv[0]!r}")
    cwd = raw.get("cwd", ".")
    if (not isinstance(cwd, str) or not cwd or cwd.startswith("/")
            or ".." in Path(cwd).parts):
        raise TrustedGateError("bad_cwd", f"{where}: {cwd!r}")
    timeout = _int(raw.get("timeout_seconds"), code="bad_timeout", lo=1, hi=MAX_TIMEOUT_S, where=where)
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


def load_trusted_gate(project_id: str) -> TrustedGate | None:
    path = gate_path(project_id)
    if not path.exists() and not path.is_symlink():
        return None
    _file_guard(path)
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


def registry_view(gate: TrustedGate) -> dict[str, dict[str, Any]]:
    """The gate as the registry shape every reader already understands, each
    spec tagged ``tier: trusted`` so the executor knows which runner to use."""
    return {c.id: {"argv": list(c.argv), "cwd": c.cwd, "timeout_seconds": c.timeout_seconds,
                   "scope": c.scope, "tier": "trusted"} for c in gate.commands}


def invalid_registry_view(project_id: str, code: str) -> dict[str, dict[str, Any]]:
    """A present-but-invalid file is LOUD: one marker command the executor turns
    into a blocked record, so the project neither loses its gate silently nor
    falls back to the sandboxed tier."""
    return {"trusted-gate": {"tier": "trusted", "invalid": code, "argv": [], "cwd": ".",
                             "timeout_seconds": 1, "scope": "unit"}}


__all__ = ["TrustedGate", "TrustedGateError", "TrustedCommand", "gate_path", "gates_dir",
           "invalid_registry_view", "load_trusted_gate", "registry_view"]
```

(Check `errorta_app.paths.errorta_home` and `errorta_export.safe_path.safe_segment` are the real names — both are used in `ledger.py`; mirror its imports.)

- [ ] **Step 4: Run** — expected PASS. Add `tests/council/__init__.py` only if the directory lacks one and collection needs it.

- [ ] **Step 5: Commit** — `git add python/errorta_council/coding/trusted_gate.py python/tests/council/test_trusted_gate.py && git commit -m "feat(gates): operator-declared trusted gate file — load, guard, validate" (+ trailer)`.

---

### Task 2: The unsandboxed runner

**Files:**
- Modify: `python/errorta_council/coding/trusted_gate.py`
- Test: `python/tests/council/test_trusted_gate.py`

**Interfaces:**
- Produces: `def run_trusted_command(spec: dict, *, command_id: str, workspace_root: Path, env_passthrough: tuple[str, ...], should_cancel=None) -> TestRunResult` (imports `TestRunResult` from `errorta_council.coding.testing` lazily inside the function to avoid an import cycle) and `def passthrough_env(names: tuple[str, ...]) -> dict[str, str]`.

- [ ] **Step 1: Failing tests** — append to `test_trusted_gate.py`:

```python
def test_passthrough_env_copies_only_listed_non_secret_names(monkeypatch) -> None:
    monkeypatch.setenv("JAVA_HOME", "/jdk")
    monkeypatch.setenv("MY_API_KEY", "s3cret")
    env = tg.passthrough_env(("JAVA_HOME", "MY_API_KEY", "NOT_SET_ANYWHERE_X"))
    assert env == {"JAVA_HOME": "/jdk"}


def test_run_trusted_command_passes_and_records_trusted(tmp_path: Path) -> None:
    res = tg.run_trusted_command({"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
                                 command_id="ok", workspace_root=tmp_path, env_passthrough=("PATH",))
    assert res.passed and res.status == "completed" and res.exit_code == 0
    assert res.command_id == "ok" and len(res.argv_sha256) == 64


def test_run_trusted_command_failure_is_a_real_red(tmp_path: Path) -> None:
    res = tg.run_trusted_command({"argv": ["/usr/bin/false"], "cwd": ".", "timeout_seconds": 5},
                                 command_id="no", workspace_root=tmp_path, env_passthrough=())
    assert not res.passed and res.status == "failed" and res.exit_code == 1 and res.reason == "exit 1"


def test_run_trusted_command_times_out_and_kills_the_group(tmp_path: Path) -> None:
    import time
    t0 = time.monotonic()
    res = tg.run_trusted_command({"argv": ["/bin/sleep", "30"], "cwd": ".", "timeout_seconds": 1},
                                 command_id="slow", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "timed_out" and not res.passed and "timed out" in res.reason
    assert time.monotonic() - t0 < 10


def test_run_trusted_command_cwd_is_inside_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    res = tg.run_trusted_command({"argv": ["/bin/pwd"], "cwd": "sub", "timeout_seconds": 5},
                                 command_id="pwd", workspace_root=tmp_path, env_passthrough=())
    assert res.passed and str((tmp_path / "sub").resolve()) in res.stdout_preview


def test_run_trusted_command_invalid_marker_is_blocked(tmp_path: Path) -> None:
    spec = tg.invalid_registry_view("reaper", "gate_mode_insecure")["trusted-gate"]
    res = tg.run_trusted_command(spec, command_id="trusted-gate", workspace_root=tmp_path, env_passthrough=())
    assert res.status == "blocked" and not res.passed and res.reason == "trusted_gate_invalid:gate_mode_insecure"


def test_run_trusted_command_cancel_before_launch(tmp_path: Path) -> None:
    res = tg.run_trusted_command({"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5},
                                 command_id="c", workspace_root=tmp_path, env_passthrough=(),
                                 should_cancel=lambda: True)
    assert res.status == "blocked" and res.reason == "cancelled before launch"
```

(`/bin/sleep`, `/bin/pwd`, `/usr/bin/true`, `/usr/bin/false` exist on macOS; if a Linux runner lacks `/usr/bin/true`, resolve with `shutil.which` in the test and skip if absent.)

- [ ] **Step 2: Run** — expected FAIL.

- [ ] **Step 3: Implement** — append to `trusted_gate.py`:

```python
import hashlib
import os as _os
import signal
import subprocess
import time

MAX_OUTPUT_BYTES = 2_000_000
PREVIEW_CHARS = 4000


def passthrough_env(names: tuple[str, ...]) -> dict[str, str]:
    """Only the listed, non-secret names, only when set — values read NOW from
    the sidecar's environment, never from the file."""
    out: dict[str, str] = {}
    for n in names:
        if is_secret_env_name(n):
            continue
        v = _os.environ.get(n)
        if v is not None:
            out[n] = v
    return out


def _tail(data: bytes) -> str:
    return data[-MAX_OUTPUT_BYTES:].decode("utf-8", "replace")[-PREVIEW_CHARS:]


def run_trusted_command(spec: dict[str, Any], *, command_id: str, workspace_root: Path,
                        env_passthrough: tuple[str, ...], should_cancel=None):
    """Run ONE trusted command with no sandbox wrapper and the declared
    passthrough environment. Records ``sandbox: trusted`` semantics through the
    session (see testing.run_test_commands); the result shape is the registry's."""
    from errorta_council.coding.testing import TestRunResult

    def _blocked(reason: str) -> "TestRunResult":
        return TestRunResult(command_id=command_id, argv_sha256="", status="blocked",
                             exit_code=None, passed=False, duration_ms=0, stdout_sha256="",
                             stdout_preview="", stderr_preview="", reason=reason)

    invalid = spec.get("invalid")
    if invalid:
        return _blocked(f"trusted_gate_invalid:{invalid}")
    if should_cancel is not None and should_cancel():
        return _blocked("cancelled before launch")
    argv = [str(a) for a in spec.get("argv", [])]
    cwd = (Path(workspace_root) / str(spec.get("cwd", "."))).resolve()
    try:
        cwd.relative_to(Path(workspace_root).resolve())
    except ValueError:
        return _blocked("cwd_outside_workspace")
    timeout = float(spec.get("timeout_seconds", 1) or 1)
    argv_sha = hashlib.sha256(repr(argv).encode()).hexdigest()
    env = passthrough_env(env_passthrough)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — operator-declared, validated argv; no shell
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return TestRunResult(command_id=command_id, argv_sha256=argv_sha, status="failed",
                             exit_code=None, passed=False, duration_ms=0, stdout_sha256="",
                             stdout_preview="", stderr_preview=str(exc)[:PREVIEW_CHARS],
                             reason=f"launch failed: {type(exc).__name__}")
    status = "completed"
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timed_out"
        try:
            _os.killpg(proc.pid, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
        except ProcessLookupError:
            out, err = b"", b""
    duration_ms = int((time.monotonic() - t0) * 1000)
    exit_code = proc.returncode if status == "completed" else None
    if status == "completed" and exit_code != 0:
        status = "failed"
    passed = status == "completed" and exit_code == 0
    reason = "" if passed else (f"timed out after {int(timeout)}s" if status == "timed_out"
                                else f"exit {exit_code}")
    return TestRunResult(command_id=command_id, argv_sha256=argv_sha, status=status,
                         exit_code=exit_code, passed=passed, duration_ms=duration_ms,
                         stdout_sha256=hashlib.sha256(out or b"").hexdigest(),
                         stdout_preview=_tail(out or b""), stderr_preview=_tail(err or b""),
                         reason=reason)
```

Add `passthrough_env`, `run_trusted_command` to `__all__`. (Check `TestRunResult` statuses used by readers — `completed | failed | timed_out | blocked` per its docstring — and that `_run_one` reports a non-zero exit as `status="failed"`; if it instead reports `completed` with a non-zero code, mirror that exactly so the two tiers read the same.)

- [ ] **Step 4: Run** — expected PASS. Then run `tests/coding/test_real_test_runs.py -q` to confirm no import cycle broke `testing`.

- [ ] **Step 5: Commit** — `fix… feat(gates): unsandboxed trusted-command runner with timeout, group kill, and passthrough env` (+ trailer).

---

### Task 3: The ledger seam — `get_test_commands` serves the trusted gate

**Files:**
- Modify: `python/errorta_council/coding/ledger.py` (`get_test_commands` ~line 1330-1343, `get_unit_test_commands`)
- Test: `python/tests/coding/test_trusted_gate_tier.py` (new)

**Interfaces:**
- `LedgerStore.get_test_commands()` returns `trusted_gate.registry_view(gate)` when `load_trusted_gate(self.project_id)` is not None; `invalid_registry_view(project_id, exc.code)` when it raises `TrustedGateError`; else the file registry as today. `get_unit_test_commands` needs no change (it filters `get_test_commands()`). `set_test_commands` is unchanged (the file registry is still written; it is simply shadowed while the operator file exists).
- Produces: `LedgerStore.gate_tier() -> str` returning `"trusted"` / `"trusted_invalid"` / `"sandboxed"` / `"none"`.

- [ ] **Step 1: Failing tests** — `python/tests/coding/test_trusted_gate_tier.py`:

```python
from __future__ import annotations
from pathlib import Path
import pytest
import yaml
from errorta_council.coding.ledger import LedgerStore


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ERRORTA_HOME", str(tmp_path))
    (tmp_path / "gates").mkdir()
    return tmp_path


def _store() -> LedgerStore:
    s = LedgerStore("reaper")
    s.create_project(title="reaper", north_star="x")   # match create_project's real signature (see ledger.py ~611)
    return s


def _trusted(home: Path, *, mode: int = 0o600, scope: str = "unit") -> Path:
    p = home / "gates" / "reaper.yaml"
    p.write_text(yaml.safe_dump({"version": 1, "created_by": "operator", "project_id": "reaper",
                                 "commands": [{"id": "compile", "argv": ["/usr/bin/true"], "cwd": ".",
                                               "timeout_seconds": 5, "scope": scope}],
                                 "env": {"passthrough": ["PATH"]}}))
    p.chmod(mode)
    return p


def test_registry_is_served_from_the_trusted_file_when_present(_home: Path) -> None:
    s = _store()
    s.set_test_commands({"unit": {"argv": ["/usr/bin/python3", "-m", "pytest"], "cwd": ".", "timeout_seconds": 60}})
    assert s.gate_tier() == "sandboxed" and "tier" not in s.get_test_commands()["unit"]
    _trusted(_home)
    reg = s.get_test_commands()
    assert list(reg) == ["compile"] and reg["compile"]["tier"] == "trusted"
    assert s.get_unit_test_commands() == reg
    assert s.gate_tier() == "trusted"


def test_acceptance_scope_is_not_a_unit_gate(_home: Path) -> None:
    s = _store()
    _trusted(_home, scope="acceptance")
    assert s.get_unit_test_commands() == {} and s.get_test_commands()["compile"]["scope"] == "acceptance"


def test_invalid_trusted_file_is_loud_not_silent(_home: Path) -> None:
    s = _store()
    s.set_test_commands({"unit": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5}})
    _trusted(_home, mode=0o666)
    reg = s.get_test_commands()
    assert list(reg) == ["trusted-gate"] and reg["trusted-gate"]["invalid"] == "gate_mode_insecure"
    assert s.gate_tier() == "trusted_invalid"


def test_no_file_no_registry_is_none(_home: Path) -> None:
    assert _store().gate_tier() == "none"
```

- [ ] **Step 2: Run** — expected FAIL (`gate_tier` missing; registry not shadowed).

- [ ] **Step 3: Implement** in `ledger.py`, in `get_test_commands`:

```python
    def get_test_commands(self) -> dict[str, Any]:
        # Trusted tier (spec 2026-08-23-trusted-gate): an operator file under
        # $ERRORTA_HOME/gates shadows the engine-written registry entirely. The
        # file is read here, on every call, so a file the operator just fixed or
        # removed takes effect without a restart, and every reader of the
        # registry -- availability, the tester seat, labels, bootstrap -- sees one
        # consistent gate without knowing which tier it is.
        from errorta_council.coding import trusted_gate as _tg
        try:
            gate = _tg.load_trusted_gate(self.project_id)
        except _tg.TrustedGateError as exc:
            return _tg.invalid_registry_view(self.project_id, exc.code)
        if gate is not None:
            return _tg.registry_view(gate)
        ...existing body unchanged...

    def gate_tier(self) -> str:
        """`trusted` | `trusted_invalid` | `sandboxed` | `none` — for labels and
        the CLI; never a decision input on its own."""
        from errorta_council.coding import trusted_gate as _tg
        try:
            if _tg.load_trusted_gate(self.project_id) is not None:
                return "trusted"
        except _tg.TrustedGateError:
            return "trusted_invalid"
        return "sandboxed" if self._read_file_registry() else "none"
```

(Name the existing registry-reading body `_read_file_registry()` so both methods share it; confirm `self.project_id` is the attribute name on `LedgerStore`.)

- [ ] **Step 4: Run** — the new file plus `tests/coding/test_spec12_in_loop_gate.py tests/cli/test_s7_testcfg.py tests/coding/test_f154_default_build_gate.py -q`. Expected PASS (those suites never create a `gates/` file under their `ERRORTA_HOME`).

- [ ] **Step 5: Commit** — `feat(gates): the ledger serves the trusted gate file as the project's registry` (+ trailer).

---

### Task 4: Executor routing, `require_sandbox` refusal, labels, and the Gradle `cannot_verify` marker

**Files:**
- Modify: `python/errorta_council/coding/testing.py` (`run_test_commands` ~158-214)
- Modify: `python/errorta_council/coding/gate_state.py` (`latest_gate_text` ~78)
- Modify: `python/errorta_council/coding/runner.py` (`_missing_build_dep` ~4648)
- Modify: `python/errorta_liverun/fixloop.py` (`_gate_label` ~1050)
- Test: `python/tests/coding/test_trusted_gate_tier.py`, `python/tests/liverun/test_fixloop.py`

**Interfaces:**
- `run_test_commands` — when **every** resolved spec carries `tier == "trusted"`: if `require_sandbox` → all results blocked `sandbox_required_by_project`; else run each via `trusted_gate.run_trusted_command(spec, command_id=cid, workspace_root=ws_root, env_passthrough=<from load_trusted_gate? no — from the spec>, should_cancel=...)`. Since the registry view carries no env list, extend `registry_view` (Task 1) to put `"env_passthrough": [..]` on every spec and read it here. Session `sandbox="trusted"`. A mixed registry (some trusted, some not) cannot happen by construction; assert it and fail closed (`blocked`, reason `mixed_gate_tiers`).
- `gate_state.latest_gate_text` prefixes each line of a `sandbox == "trusted"` session with `[trusted, unsandboxed] `.
- `_missing_build_dep`: `gradlew` / `mvnw` argv0 → marker `.gradle` / `.m2` under `HOME`? No — simpler and honest: when `argv[0]` endswith `gradlew` or `mvnw` and the spec is **not** trusted, return `("gradle"|"maven", "trusted gate required")` so the sandboxed path degrades to `cannot_verify` with that text instead of a hard red.
- `fixloop._gate_label` appends ` [trusted, unsandboxed]` when the first spec has `tier == "trusted"`.

- [ ] **Step 1: Failing tests** — append to `test_trusted_gate_tier.py`:

```python
from errorta_council.coding.testing import run_test_commands


def test_trusted_registry_runs_unsandboxed_and_records_trusted(_home: Path, tmp_path: Path) -> None:
    s = _store(); _trusted(_home)
    reg = s.get_test_commands()
    ws = tmp_path / "ws"; ws.mkdir()
    session = run_test_commands(ws, reg, list(reg), require_sandbox=False)
    assert session.passed and session.sandbox == "trusted"
    assert session.results[0].command_id == "compile" and session.results[0].status == "completed"


def test_require_sandbox_refuses_a_trusted_gate(_home: Path, tmp_path: Path) -> None:
    s = _store(); _trusted(_home)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg), require_sandbox=True)
    assert not session.passed and session.sandbox == "trusted"
    assert session.results[0].status == "blocked" and session.results[0].reason == "sandbox_required_by_project"


def test_invalid_trusted_file_blocks_the_gate_loudly(_home: Path, tmp_path: Path) -> None:
    s = _store(); _trusted(_home, mode=0o666)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg))
    assert not session.passed and session.results[0].reason == "trusted_gate_invalid:gate_mode_insecure"


def test_sandboxed_registry_path_is_untouched(tmp_path: Path) -> None:
    reg = {"t": {"argv": ["/usr/bin/true"], "cwd": ".", "timeout_seconds": 5}}
    session = run_test_commands(tmp_path, reg, ["t"], sandbox="none")
    assert session.passed and session.sandbox == "none"


def test_gate_text_names_the_tier(_home: Path, tmp_path: Path) -> None:
    from errorta_council.coding import gate_state
    s = _store(); _trusted(_home)
    reg = s.get_test_commands()
    session = run_test_commands(tmp_path, reg, list(reg))
    s.record_test_run(session, task_id="t1", head="abc")
    assert "[trusted, unsandboxed]" in gate_state.latest_gate_text(s)


def test_gradle_in_the_sandboxed_tier_degrades_to_cannot_verify(tmp_path: Path) -> None:
    from errorta_council.coding.runner import _missing_build_dep
    assert _missing_build_dep(["./gradlew", "build"], ".", tmp_path) == ("gradle", "trusted gate required")
    assert _missing_build_dep(["./mvnw", "test"], ".", tmp_path) == ("maven", "trusted gate required")
```

and in `tests/liverun/test_fixloop.py`:

```python
def test_gate_label_names_a_trusted_tier() -> None:
    from errorta_liverun.fixloop import _gate_label
    class _S:
        def get_test_commands(self):
            return {"compile": {"argv": ["./gradlew", "build"], "tier": "trusted"}}
    assert _gate_label(_S()) == "compile — ./gradlew build [trusted, unsandboxed]"
```

- [ ] **Step 2: Run** — expected FAIL.

- [ ] **Step 3: Implement.** `testing.run_test_commands`, after `resolved, unknown = resolve_commands(...)`:

```python
    tiers = {str(spec.get("tier") or "sandboxed") for _cid, spec in resolved}
    if "trusted" in tiers:
        from errorta_council.coding import trusted_gate as _tg
        if tiers != {"trusted"}:
            blocked = [_blocked_result(cid, "mixed_gate_tiers") for cid, _ in resolved]
            return TestRunSession(command_ids=list(command_ids), results=blocked,
                                  unknown_ids=unknown, passed=False, sandbox="trusted")
        if require_sandbox:
            blocked = [_blocked_result(cid, "sandbox_required_by_project") for cid, _ in resolved]
            return TestRunSession(command_ids=list(command_ids), results=blocked,
                                  unknown_ids=unknown, passed=False, sandbox="trusted")
        results = []
        for cid, spec in resolved:
            results.append(_tg.run_trusted_command(
                spec, command_id=cid, workspace_root=ws_root,
                env_passthrough=tuple(spec.get("env_passthrough") or ()),
                should_cancel=should_cancel))
            if results[-1].status in ("blocked", "timed_out"):
                break
        passed = bool(command_ids) and not unknown and bool(resolved) and all(r.passed for r in results)
        return TestRunSession(command_ids=list(command_ids), results=results,
                              unknown_ids=unknown, passed=passed, sandbox="trusted")
```

with a small `_blocked_result(cid, reason)` helper (factor the existing `sandbox_unavailable` construction through it). `registry_view` gains `"env_passthrough": list(gate.env_passthrough)` on each spec (update Task 1's `test_registry_views` expectation accordingly — the implementer of this task edits that assertion). `gate_state.latest_gate_text`: read `sandbox` off each run record (check the record shape `store.record_test_run` persists — `TestRunSession.to_dict()`), and prefix. `_missing_build_dep`: add the two markers before the npm/cargo/go checks. `_gate_label`: append the suffix.

- [ ] **Step 4: Run** — `tests/coding/test_trusted_gate_tier.py tests/council/test_trusted_gate.py tests/coding/test_real_test_runs.py tests/coding/test_spec12_in_loop_gate.py tests/coding/test_f154_default_build_gate.py tests/liverun/test_fixloop.py -q`. Expected PASS (the F154 suite must still pass — if a test there asserts the old behaviour for a `gradlew` argv, update it to the new `cannot_verify` text; there should be none).

- [ ] **Step 5: Commit** — `feat(gates): run trusted specs unsandboxed; require_sandbox refuses; tier in gate text and labels; gradle degrades to cannot_verify in the sandboxed tier` (+ trailer).

---

### Task 5: Nothing writes to `gates/`; read-only route + CLI

**Files:**
- Create: `python/tests/test_gates_dir_is_operator_only.py`
- Modify: `python/errorta_app/routes/coding.py` (add `GET /coding/projects/{project_id}/trusted-gate` near the test-commands routes ~3389)
- Create: `python/errorta_cli/commands/trusted_gate.py`; register in `python/errorta_cli/app.py` like `testcfg`
- Test: `python/tests/test_coding_routes.py` (or the file that covers `/test-commands`), `python/tests/cli/test_trusted_gate_command.py`

- [ ] **Step 1: Grep test** —

```python
"""The gates directory is the operator's. No engine package may write there."""
from __future__ import annotations
import re
from pathlib import Path

_PKGS = ("errorta_council", "errorta_app", "errorta_slack", "errorta_liverun", "errorta_cli")
_WRITE = re.compile(r"gates_dir\(\)[^\n]*(write_text|open\(|mkdir|unlink|rename|replace)|"
                    r"[\"']gates[\"'][^\n]*(write_text|open\([^)]*[\"']w|mkdir|unlink|rename)")


def test_no_engine_code_writes_under_gates() -> None:
    root = Path(__file__).resolve().parents[1]
    hits = []
    for pkg in _PKGS:
        for py in (root / pkg).rglob("*.py"):
            for i, line in enumerate(py.read_text().splitlines(), 1):
                if _WRITE.search(line):
                    hits.append(f"{py.relative_to(root)}:{i}: {line.strip()}")
    assert not hits, "\n".join(hits)
```

- [ ] **Step 2: Route** — `GET /coding/projects/{project_id}/trusted-gate` → `{"tier": store.gate_tier(), "path": str(gate_path(project_id)), "present": bool, "valid": bool, "code": <error code or "">, "commands": [{"id","argv","cwd","timeout_seconds","scope"}], "env_passthrough": [...]}`. Same origin/token guard as the neighbouring GET routes (`_require_tauri_origin` if the GET test-commands route uses it; mirror exactly). One test per tier in the routes test file (create the file under `ERRORTA_HOME = tmp_path` like Task 3's helper).

- [ ] **Step 3: CLI** — `errorta trusted-gate [<project>]` (project defaults the way `errorta testcfg` resolves it — read `_base.py`/`testcfg.py`): prints `tier`, the file path, and either the command table or `invalid: <code>` / `none`. `--json` passes the payload through. Two tests (valid / invalid) using the CLI test harness `tests/cli/test_s7_testcfg.py` uses.

- [ ] **Step 4: Run** — the three new/changed test files + `tests/test_coding_routes.py tests/cli -q`. Expected PASS.

- [ ] **Step 5: Commit** — `feat(gates): read-only trusted-gate route and CLI; grep test that engine code never writes to gates/` (+ trailer).

---

### Task 6: Worktree copy skips build outputs; docs; example file

**Files:**
- Modify: `python/errorta_tools/runner/apply_workspace.py` (`_SKIPPED_DIR_NAMES` ~48)
- Create: `docs/gates/example-trusted-gate.yaml`
- Modify: `docs/liverun/README.md` (the "Why `osrs-reaper` ships as `fixable: false`" section; the follow-ups list)
- Test: the existing apply-workspace test file (grep `_copy_ignore` under `tests/`)

- [ ] **Step 1: Test** — add a test beside the existing `_copy_ignore` tests: a source tree with `build/`, `.gradle/`, `target/` directories and a `foo.jar` file; `ensure()` copies none of the three directories and does copy `foo.jar` (jars in `libs/` are legitimate inputs; only the build dirs are outputs).

- [ ] **Step 2: Implement** — add `"build", ".gradle", "target"` to `_SKIPPED_DIR_NAMES` with a comment: Gradle/Maven outputs; a trusted gate rebuilds them in the worktree from a warm `~/.gradle`. (If the repo has a test asserting the exact frozenset contents, update it.)

- [ ] **Step 3: Example file** — `docs/gates/example-trusted-gate.yaml`:

```yaml
# Trusted, UNSANDBOXED acceptance gate -- the operator's file, never the engine's.
# Copy to $ERRORTA_HOME/gates/<project_id>.yaml, chmod 600, fill every FILL line.
version: 1
created_by: operator
project_id: FILL            # must equal the filename stem
commands:
  - id: compile
    argv: ["./gradlew", ":client:compileJava", "--offline", "--console=plain", "-q"]
    cwd: "."
    timeout_seconds: 900
    scope: unit
  - id: unit-tests
    argv: ["./gradlew", ":client:runUnitTests", "--offline", "--console=plain", "-q"]
    cwd: "."
    timeout_seconds: 1500
    scope: unit
env:
  passthrough: [PATH, HOME, JAVA_HOME, GRADLE_USER_HOME, GRADLE_OPTS, LANG, TMPDIR]
```

- [ ] **Step 4: README** — replace the "Why `osrs-reaper` ships as `fixable: false`" section with "Giving a project a trusted gate": what the file is, the provenance bar (same as a profile), that the engine never writes it, `--offline` and `HOME` rationale, `errorta trusted-gate <project>` to check it, the 1800 s ceiling, that `require_sandbox` and a trusted gate refuse each other, and the final step: set `fixable: true` in the live-run profile once the CLI shows the gate valid. Remove the reaper bullet from "Follow-ups".

- [ ] **Step 5: Run** — the apply-workspace tests + `tests/liverun -q`. Commit — `feat(gates): worktree copy skips build outputs; trusted-gate docs and example` (+ trailer).

---

## Self-review (done while writing)

- Spec trust model → Task 1; execution → Tasks 2+4; availability/ledger seam → Task 3; `require_sandbox` refusal → Task 4; invalid-file loudness → Tasks 1, 3, 4; labels → Task 4; operator surface → Task 5; grep test → Task 5; worktree note + docs + example → Task 6; `_missing_build_dep` marker → Task 4.
- Names consistent across tasks: `TrustedGate`, `TrustedGateError.code`, `load_trusted_gate`, `registry_view` (with `env_passthrough` added in Task 4), `invalid_registry_view`, `run_trusted_command`, `passthrough_env`, `LedgerStore.gate_tier`, session `sandbox == "trusted"`, reasons `trusted_gate_invalid:<code>`, `sandbox_required_by_project`, `mixed_gate_tiers`.
- The live measurement (author the real file, seed the reaper worktree, one run, flip `fixable`) is done by the session after merge; not a task.
